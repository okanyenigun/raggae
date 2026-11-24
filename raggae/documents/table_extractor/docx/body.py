"""Tables declared in a Word document's main body, with python-docx."""

import re
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import (
    failure,
    finding,
    finish,
    nothing_selected,
    office_package,
    read_source,
    select_table_regions,
)
from ...layout_analysis.models import (
    DocxElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...normalization.artifacts import Artifact
from ...shared.docx_text import (
    W,
    children,
    paragraph_text,
    resolve,
    table_text,
    unreadable,
)
from ...shared.sources import as_artifact
from ...text_reader.models import FindingCode

# Parts whose tables are outside the main body.
_OTHER_PARTS = frozenset({"header", "footer", "footnotes", "endnotes", "comments"})
# A Word table's start tag, whatever its namespace prefix.
_TABLE_TAG = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?tbl[\s>/]")
_FALSE = frozenset({"0", "false", "off"})


class DocxTableExtractor:
    """Return the tables declared in a Word document's main body, with python-docx.

    Without a layout: every body table in body order, tables inside content
    controls included. Each is an ``ExtractedTable`` with
    ``method="declared"`` and ``page_index=0`` (a .docx stores no pages),
    its grid built from Word's table model: ``w:gridSpan`` is a column span,
    a ``w:vMerge`` that restarts and then continues is a row span, and
    ``w:gridBefore`` leaves leading grid columns uncovered. The grid is as
    wide as the table's declared grid (``w:tblGrid``) or its widest row. The
    leading rows Word repeats as header rows (``w:tblHeader``) hold header
    cells. ``caption`` is the table's alt-text title (``w:tblCaption``), else
    its alt-text description (``w:tblDescription``).

    A cell's text is its paragraphs as ``DocxTextReader`` reads them, one per
    line: tracked changes as if accepted, image alt text included, empty
    paragraphs left out. A table inside a cell is read as text in that cell,
    one row per line with cells joined by `` | `` (``nested_table``). Deleted
    rows are left out (``revisions_present``). Text in a cell that a vertical
    merge continues is added to the merged cell. A table with no text in any
    cell is not returned (``empty_table``). Legacy horizontal merges
    (``w:hMerge``) are not merged.

    Not read, each with one informational finding: tables in headers,
    footers, footnotes, endnotes, or comments (``other_parts_unread``) and in
    text boxes (``text_boxes_unread``).

    With a layout: the tables at or inside each requested element, located as
    ``DocxLayoutAnalyzer`` locates them. Without ``region_ids`` only table
    regions are used; explicit ``region_ids`` of any kind give the tables
    inside them. As in the text reader, content inside another selected
    region belongs only to that region (a selected picture's alt text leaves
    its cell), regions locating the same element are both refused
    (``selection_overlap``), and a region the text reader cannot read gets
    ``selection_unsupported``. One without a table gets ``empty``.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.NESTED_TABLE,
            TableFindingCode.REVISIONS_PRESENT,
            TableFindingCode.OTHER_PARTS_UNREAD,
            TableFindingCode.TEXT_BOXES_UNREAD,
            TableFindingCode.UNSUPPORTED_CONTENT,
        }
    )
    _NOTES = {
        TableFindingCode.NESTED_TABLE: (
            "A table inside a table cell was read as text in that cell."
        ),
        TableFindingCode.REVISIONS_PRESENT: (
            "Tracked changes were read as if accepted: deleted rows and text left out."
        ),
        TableFindingCode.OTHER_PARTS_UNREAD: (
            "Tables in headers, footers, footnotes, endnotes, or comments were not "
            "extracted; only the main body is read."
        ),
        TableFindingCode.TEXT_BOXES_UNREAD: "Text boxes were not read.",
        TableFindingCode.UNSUPPORTED_CONTENT: (
            "Embedded objects, equations, or imported content in cells were not read."
        ),
    }

    @property
    def name(self) -> str:
        return "table_extractor_docx"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TableExtractResult:
        artifact = as_artifact(source)
        selected = select_table_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name, explicit=region_ids is not None)
        data = read_source(artifact, self.name)
        if isinstance(data, TableExtractResult):
            return data
        if (refused := office_package(data, self.name)) is not None:
            return refused
        try:
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
            from lxml.etree import LxmlError
        except ImportError as error:
            raise ImportError(
                "Word table extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            document = Document(BytesIO(data))
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            # python-docx raises these on some malformed package parts.
            AttributeError,
            TypeError,
        ):
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be opened as a Word document.",
            )
        try:
            if selected is None:
                return self._standalone(document)
            return self._regions(document, selected)
        except RecursionError:
            return failure(
                self.name,
                TableFindingCode.STRUCTURE_UNSUPPORTED,
                "Word structure is too deeply nested to read within Python's "
                "recursion limit.",
            )

    def _standalone(self, document: Any) -> TableExtractResult:
        body = document.element.body
        found = DocxTableExtractor._tables(body, frozenset())
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        notes: set[TableFindingCode] = set()
        for element in found:
            DocxTableExtractor._read(
                element, None, frozenset(), tables, findings, notes
            )
        if any(
            next(box.iter(f"{W}tbl"), None) is not None
            for box in body.iter(f"{W}txbxContent")
        ):
            notes.add(TableFindingCode.TEXT_BOXES_UNREAD)
        if any(
            not relation.is_external
            and relation.reltype.rsplit("/", 1)[-1] in _OTHER_PARTS
            and _TABLE_TAG.search(relation.target_part.blob) is not None
            for relation in document.part.rels.values()
        ):
            notes.add(TableFindingCode.OTHER_PARTS_UNREAD)
        if not found:
            findings.append(
                finding(TableFindingCode.EMPTY, "No tables in the document body.", 0)
            )
        findings.extend(DocxTableExtractor._note(code, None) for code in sorted(notes))
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, document: Any, selected: tuple[LayoutRegion, ...]
    ) -> TableExtractResult:
        part = str(document.part.partname).lstrip("/")
        body = document.element.body
        findings: list[TableExtractFinding] = []
        resolved: list[tuple[LayoutRegion, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, DocxElementLocation):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "Selection is not a Word element.",
                        region_id=region.id,
                    )
                )
                continue
            element = (
                resolve(body, location.child_indexes) if location.part == part else None
            )
            if element is None:
                findings.append(
                    finding(
                        TableFindingCode.REGION_UNRESOLVED,
                        "The region's location was not found in this document.",
                        0,
                        region.id,
                    )
                )
            elif reason := unreadable(element):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        reason,
                        region_id=region.id,
                    )
                )
            else:
                resolved.append((region, element))

        counts: dict[int, int] = {}
        for _, element in resolved:
            counts[id(element)] = counts.get(id(element), 0) + 1
        owners = frozenset(id(element) for _, element in resolved)
        tables: list[ExtractedTable] = []
        completed = 0
        for region, element in resolved:
            if counts[id(element)] > 1:
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_OVERLAP,
                        "Selected regions locate the same element; neither was extracted.",
                        0,
                        region.id,
                    )
                )
                continue
            completed += 1
            excluded = owners - {id(element)}
            found = DocxTableExtractor._tables(element, excluded)
            notes: set[TableFindingCode] = set()
            if not found:
                findings.append(
                    finding(
                        TableFindingCode.EMPTY, "No table in this region.", 0, region.id
                    )
                )
            for table in found:
                DocxTableExtractor._read(
                    table, region.id, excluded, tables, findings, notes
                )
            findings.extend(
                DocxTableExtractor._note(code, region.id) for code in sorted(notes)
            )
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _tables(node: Any, excluded: frozenset[int]) -> list[Any]:
        """The table at ``node``, or the tables in it at body level (through
        content controls), leaving out those owned by other regions."""
        if node.tag == f"{W}tbl":
            return [node]
        return [
            table for table in children(node, f"{W}tbl") if id(table) not in excluded
        ]

    @staticmethod
    def _read(
        element: Any,
        region_id: str | None,
        excluded: frozenset[int],
        tables: list[ExtractedTable],
        findings: list[TableExtractFinding],
        notes: set[TableFindingCode],
    ) -> None:
        table = DocxTableExtractor._grid(element, region_id, excluded, notes)
        if table is None:
            findings.append(
                finding(
                    TableFindingCode.EMPTY_TABLE,
                    "A table with no text in any cell was not returned.",
                    0,
                    region_id,
                )
            )
        else:
            tables.append(table)

    @staticmethod
    def _grid(
        table: Any,
        region_id: str | None,
        excluded: frozenset[int],
        notes: set[TableFindingCode],
    ) -> ExtractedTable | None:
        """The table's grid from Word's table model; None when no cell has text."""
        read: set[FindingCode] = set()
        rows: list[Any] = []
        for row in children(table, f"{W}tr"):
            properties = row.find(f"{W}trPr")
            if properties is not None and properties.find(f"{W}del") is not None:
                read.add(FindingCode.REVISIONS_PRESENT)
                continue
            rows.append(row)
        headers = 0
        for row in rows:
            if not DocxTableExtractor._is_header(row):
                break
            headers += 1
        # Each cell as [row, column, row_span, column_span, text]; a vertical
        # merge continues the cell that starts above at the same columns.
        cells: list[list[Any]] = []
        open_merges: dict[int, int] = {}
        widest = 0
        for y, row in enumerate(rows):
            row_elsewhere = DocxTableExtractor._elsewhere(row, table, excluded)
            continued: dict[int, int] = {}
            x = DocxTableExtractor._number(row, ("trPr", "gridBefore"), 0)
            for cell in children(row, f"{W}tc"):
                span = max(1, DocxTableExtractor._number(cell, ("tcPr", "gridSpan"), 1))
                merge = DocxTableExtractor._vertical_merge(cell)
                text = (
                    ""
                    if row_elsewhere or id(cell) in excluded
                    else DocxTableExtractor._cell_text(cell, excluded, read, notes)
                )
                origin = open_merges.get(x)
                if (
                    merge == "continue"
                    and origin is not None
                    and cells[origin][3] == span
                ):
                    cells[origin][2] += 1
                    if text:
                        joined = (
                            f"{cells[origin][4]}\n{text}" if cells[origin][4] else text
                        )
                        cells[origin][4] = joined
                    continued[x] = origin
                else:
                    if merge == "restart":
                        continued[x] = len(cells)
                    cells.append([y, x, 1, span, text])
                x += span
            open_merges = continued
            widest = max(widest, x)
        notes.update(TableFindingCode(code.value) for code in read)
        if not any(str(record[4]).strip() for record in cells):
            return None
        grid = table.find(f"{W}tblGrid")
        declared = 0 if grid is None else len(grid.findall(f"{W}gridCol"))
        return ExtractedTable(
            cells=tuple(
                TableCell(
                    row=y,
                    column=x,
                    row_span=row_span,
                    column_span=column_span,
                    text=text,
                    header=y < headers,
                )
                for y, x, row_span, column_span, text in cells
            ),
            row_count=len(rows),
            column_count=max(declared, widest),
            page_index=0,
            region_id=region_id,
            method="declared",
            caption=DocxTableExtractor._caption(table),
        )

    @staticmethod
    def _cell_text(
        cell: Any,
        excluded: frozenset[int],
        read: set[FindingCode],
        notes: set[TableFindingCode],
    ) -> str:
        """The cell's paragraphs and nested tables as the text reader reads them,
        one paragraph (or nested-table row) per line."""
        parts: list[str] = []
        for child in children(cell, f"{W}p", f"{W}tbl"):
            if id(child) in excluded:
                continue
            if child.tag == f"{W}p":
                text = paragraph_text(child, excluded, read)
            else:
                text = table_text(child, excluded, read)
                if text is not None:
                    notes.add(TableFindingCode.NESTED_TABLE)
            if text is not None and text.strip():
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _is_header(row: Any) -> bool:
        properties = row.find(f"{W}trPr")
        marker = None if properties is None else properties.find(f"{W}tblHeader")
        return marker is not None and marker.get(f"{W}val", "true") not in _FALSE

    @staticmethod
    def _vertical_merge(cell: Any) -> str | None:
        properties = cell.find(f"{W}tcPr")
        merge = None if properties is None else properties.find(f"{W}vMerge")
        if merge is None:
            return None
        return "restart" if merge.get(f"{W}val") == "restart" else "continue"

    @staticmethod
    def _number(node: Any, path: tuple[str, str], default: int) -> int:
        """An integer ``w:val`` at ``node/w:{path[0]}/w:{path[1]}``, or ``default``.

        Capped at HTML's colspan ceiling, 1000 (Word itself stops at 63 columns).
        """
        properties = node.find(f"{W}{path[0]}")
        element = None if properties is None else properties.find(f"{W}{path[1]}")
        value = None if element is None else element.get(f"{W}val")
        digits = "" if value is None else value.strip().lstrip("0") or "0"
        if not digits.isdigit():
            return default
        return 1000 if len(digits) > 4 else min(int(digits), 1000)

    @staticmethod
    def _caption(table: Any) -> str | None:
        properties = table.find(f"{W}tblPr")
        if properties is None:
            return None
        for name in ("tblCaption", "tblDescription"):
            element = properties.find(f"{W}{name}")
            value = None if element is None else element.get(f"{W}val")
            if value:
                return value
        return None

    @staticmethod
    def _elsewhere(row: Any, table: Any, excluded: frozenset[int]) -> bool:
        """Whether the row, or a wrapper between it and its table, is another region."""
        node = row
        while node is not None and node is not table:
            if id(node) in excluded:
                return True
            node = node.getparent()
        return False

    @staticmethod
    def _note(code: TableFindingCode, region_id: str | None) -> TableExtractFinding:
        return finding(code, DocxTableExtractor._NOTES[code], 0, region_id)
