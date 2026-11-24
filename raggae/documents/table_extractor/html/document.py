"""Tables declared in an HTML document, with BeautifulSoup."""

import re
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args
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
    read_source,
    select_table_regions,
)
from ...layout_analysis.models import (
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding
from ...shared.html_text import (
    SKIPPED,
    UNSUPPORTED,
    element_text,
    lineage,
    resolve,
    visible_in,
)
from ...shared.sources import as_artifact
from ...text_reader.models import FindingCode


class HtmlTableExtractor:
    """Return the tables declared in an HTML document, with BeautifulSoup.

    Without a layout: every outermost ``<table>``, in document order. Each is
    an ``ExtractedTable`` with ``method="declared"`` and ``page_index=0``,
    its grid built as a browser builds it: ``colspan`` and ``rowspan`` become
    spans (``rowspan="0"`` reaches the end of its row group, and no span goes
    past it), ``<th>`` cells are header cells, and ``caption`` is the first
    ``<caption>``. Rows come in source order, from ``<thead>``, ``<tbody>``,
    and ``<tfoot>`` alike. Short rows are not padded and empty rows are kept.
    A cell whose span would run into another cell is shortened
    (``span_shortened``).

    A cell's text is what ``HtmlTextReader`` reads from it: whitespace
    collapsed as a browser does, ``<br>`` and block elements as line breaks
    (kept as ``"\\n"``), image alt text included. A table inside a cell is read
    as text in that cell, one row per line with cells joined by `` | ``
    (``nested_table``). A table with no text in any cell is not returned
    (``empty_table``).

    As in the HTML text reader: scripts, styles, ``<template>``,
    ``<noscript>``, and ``<head>`` are never read; elements with the ``hidden``
    attribute, rows and cells included, are skipped (``hidden_skipped``; CSS is
    not applied); embedded, interactive, or vector elements are not read
    (``unsupported_content``). The source is decoded with ``encoding`` (UTF-8
    by default), never guessed from ``<meta charset>``.

    With a layout: the tables at or inside each requested element, located as
    ``HtmlLayoutAnalyzer`` locates them (element-only child indexes, tag
    checked). Without ``region_ids`` only table regions are used; explicit
    ``region_ids`` of any kind give the tables inside them. As in the text
    reader, content inside another selected region belongs only to that
    region, and regions locating the same element are both refused
    (``selection_overlap``). A hidden region is counted, with
    ``hidden_skipped``; a region in never-read content or on an embedded,
    interactive, or vector element gets ``selection_unsupported``; one
    without a table, ``empty``.
    """

    # A span value as HTML reads it: leading whitespace, an optional "+", digits;
    # anything after the digits is ignored ("2px" is 2).
    _SPAN = re.compile(r"[\t\n\f\r ]*\+?([0-9]+)")
    # HTML's ceilings: colspan above 1000 is 1000, rowspan above 65534 is 65534.
    _MAX_COLSPAN = 1000
    _MAX_ROWSPAN = 65534

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.HIDDEN_SKIPPED,
            TableFindingCode.UNSUPPORTED_CONTENT,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.NESTED_TABLE,
            TableFindingCode.SPAN_SHORTENED,
        }
    )
    _NOTES = {
        TableFindingCode.HIDDEN_SKIPPED: "Elements marked hidden were not read.",
        TableFindingCode.UNSUPPORTED_CONTENT: (
            "Embedded, interactive, or vector elements were not read."
        ),
        TableFindingCode.NESTED_TABLE: (
            "A table inside a table cell was read as text in that cell."
        ),
        TableFindingCode.SPAN_SHORTENED: (
            "A cell's span ran into another cell and was shortened."
        ),
    }

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "table_extractor_html"

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
        data = read_source(artifact, self.name, allow_empty=True)
        if isinstance(data, TableExtractResult):
            return data
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise ImportError(
                "HTML table extraction requires: pip install 'raggae[text-html]'"
            ) from error
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                document = BeautifulSoup(text, "html.parser")
            if selected is None:
                return self._standalone(document)
            return self._regions(document, selected)
        except RecursionError:
            return failure(
                self.name,
                TableFindingCode.STRUCTURE_UNSUPPORTED,
                "HTML nesting is too deep to read within Python's recursion limit.",
            )
        except (ValueError, AssertionError):
            return failure(
                self.name,
                TableFindingCode.PARSE_FAILED,
                "Source could not be parsed as HTML.",
            )

    def _standalone(self, document: Any) -> TableExtractResult:
        found, notes = HtmlTableExtractor._tables(document, frozenset())
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        if not found and not notes:
            findings.append(
                finding(TableFindingCode.EMPTY, "No tables in the document.", 0)
            )
        for element in found:
            HtmlTableExtractor._read(
                element, None, frozenset(), tables, findings, notes
            )
        findings.extend(HtmlTableExtractor._note(code, None) for code in sorted(notes))
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, document: Any, selected: tuple[LayoutRegion, ...]
    ) -> TableExtractResult:
        findings: list[TableExtractFinding] = []
        resolved: list[tuple[LayoutRegion, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, HtmlElementLocation):
                findings.append(
                    HtmlTableExtractor._unsupported(
                        region, "Selection is not an HTML element."
                    )
                )
                continue
            element = resolve(document, location.child_indexes)
            if element is None or element.name != location.tag:
                findings.append(
                    finding(
                        TableFindingCode.REGION_UNRESOLVED,
                        "The region's location was not found in this document.",
                        0,
                        region.id,
                    )
                )
            elif element.name in UNSUPPORTED or any(
                node.name in SKIPPED for node in lineage(element)
            ):
                findings.append(
                    HtmlTableExtractor._unsupported(
                        region,
                        "The region is never-read content or an embedded, interactive, "
                        "or vector element.",
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
            if any(node.has_attr("hidden") for node in lineage(element)):
                findings.append(
                    HtmlTableExtractor._note(TableFindingCode.HIDDEN_SKIPPED, region.id)
                )
                continue
            excluded = owners - {id(element)}
            found, notes = HtmlTableExtractor._tables(element, excluded)
            if not found and not notes:
                findings.append(
                    finding(
                        TableFindingCode.EMPTY, "No table in this region.", 0, region.id
                    )
                )
            for table in found:
                HtmlTableExtractor._read(
                    table, region.id, excluded, tables, findings, notes
                )
            findings.extend(
                HtmlTableExtractor._note(code, region.id) for code in sorted(notes)
            )
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _tables(
        node: Any, excluded: frozenset[int]
    ) -> tuple[list[Any], set[TableFindingCode]]:
        """The outermost read ``<table>`` elements at or under ``node``, in order,
        and notes for tables inside hidden or unsupported elements."""
        from bs4.element import Tag

        found: list[Any] = []
        notes: set[TableFindingCode] = set()
        pending = [node]
        while pending:
            item = pending.pop()
            if (
                not isinstance(item, Tag)
                or item.name in SKIPPED
                or id(item) in excluded
            ):
                continue
            holds_table = item.name == "table" or item.find("table") is not None
            if item.has_attr("hidden"):
                if holds_table:
                    notes.add(TableFindingCode.HIDDEN_SKIPPED)
                continue
            if item.name in UNSUPPORTED:
                if holds_table:
                    notes.add(TableFindingCode.UNSUPPORTED_CONTENT)
                continue
            if item.name == "table":
                found.append(item)
                continue
            pending.extend(
                reversed([child for child in item.children if isinstance(child, Tag)])
            )
        return found, notes

    @staticmethod
    def _read(
        element: Any,
        region_id: str | None,
        excluded: frozenset[int],
        tables: list[ExtractedTable],
        findings: list[TableExtractFinding],
        notes: set[TableFindingCode],
    ) -> None:
        table = HtmlTableExtractor._grid(element, region_id, excluded, notes)
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
        """The table's grid, as the HTML table model places cells; None without text."""
        from bs4.element import Tag

        read: set[FindingCode] = set()
        rows = [
            row
            for row in table.find_all("tr")
            if row.find_parent("table") is table and visible_in(row, table, read)
        ]
        # The last row of each row's group: rows sharing a parent, one after another.
        group_end = list(range(len(rows)))
        for index in reversed(range(len(rows) - 1)):
            if rows[index + 1].parent is rows[index].parent:
                group_end[index] = group_end[index + 1]
        busy: list[int] = []  # Per column, the last row a placed cell covers.
        cells: list[TableCell] = []
        for y, row in enumerate(rows):
            row_elsewhere = HtmlTableExtractor._elsewhere(row, table, excluded)
            x = 0
            for cell in row.children:
                if (
                    not isinstance(cell, Tag)
                    or cell.name not in ("td", "th")
                    or not visible_in(cell, table, read)
                ):
                    continue
                while x < len(busy) and busy[x] >= y:
                    x += 1
                colspan = HtmlTableExtractor._span(
                    cell.get("colspan"), HtmlTableExtractor._MAX_COLSPAN
                )
                rowspan = HtmlTableExtractor._span(
                    cell.get("rowspan"), HtmlTableExtractor._MAX_ROWSPAN
                )
                colspan = colspan or 1  # Absent, invalid, or zero.
                last = (
                    group_end[y]
                    if rowspan == 0
                    else min(y + (rowspan or 1) - 1, group_end[y])
                )
                width = 0
                while width < colspan and (
                    x + width >= len(busy) or busy[x + width] < y
                ):
                    width += 1
                if width < colspan:
                    notes.add(TableFindingCode.SPAN_SHORTENED)
                busy.extend([-1] * (x + width - len(busy)))
                for column in range(x, x + width):
                    busy[column] = last
                if row_elsewhere or id(cell) in excluded:
                    text = ""  # The cell's content belongs to another selected region.
                else:
                    text = element_text(cell, excluded, read) or ""
                    if HtmlTableExtractor._holds_table(cell, excluded):
                        notes.add(TableFindingCode.NESTED_TABLE)
                cells.append(
                    TableCell(
                        row=y,
                        column=x,
                        row_span=last - y + 1,
                        column_span=width,
                        text=text,
                        header=cell.name == "th",
                    )
                )
                x += width
        caption = HtmlTableExtractor._caption(table, excluded, read)
        notes.update(TableFindingCode(code.value) for code in read)
        if not any(cell.text.strip() for cell in cells):
            return None
        return ExtractedTable(
            cells=tuple(cells),
            row_count=len(rows),
            column_count=max(cell.column + cell.column_span for cell in cells),
            page_index=0,
            region_id=region_id,
            method="declared",
            caption=caption,
        )

    @staticmethod
    def _span(value: Any, maximum: int) -> int | None:
        """A colspan or rowspan as HTML parses it; None when absent or invalid."""
        match = (
            HtmlTableExtractor._SPAN.match(value) if isinstance(value, str) else None
        )
        if match is None:
            return None
        digits = match.group(1).lstrip("0")
        if len(digits) > len(str(maximum)):
            return maximum
        return min(int(digits or "0"), maximum)

    @staticmethod
    def _caption(
        table: Any, excluded: frozenset[int], read: set[FindingCode]
    ) -> str | None:
        """The text of the table's first ``<caption>``, when it is read."""
        from bs4.element import Tag

        for child in table.children:
            if isinstance(child, Tag) and child.name == "caption":
                if id(child) in excluded:
                    return None
                if child.has_attr("hidden"):
                    read.add(FindingCode.HIDDEN_SKIPPED)
                    return None
                return element_text(child, excluded, read)
        return None

    @staticmethod
    def _holds_table(cell: Any, excluded: frozenset[int]) -> bool:
        """Whether a table inside the cell is read as part of its text."""
        for inner in cell.find_all("table"):
            node = inner
            while node is not cell:
                if (
                    node.name in SKIPPED
                    or node.name in UNSUPPORTED
                    or node.has_attr("hidden")
                    or id(node) in excluded
                ):
                    break
                node = node.parent
            else:
                return True
        return False

    @staticmethod
    def _elsewhere(row: Any, table: Any, excluded: frozenset[int]) -> bool:
        """Whether the row, or an element between it and its table, is another region."""
        node = row
        while node is not None and node is not table:
            if id(node) in excluded:
                return True
            node = node.parent
        return False

    @staticmethod
    def _unsupported(region: LayoutRegion, message: str) -> TableExtractFinding:
        return finding(
            TableFindingCode.SELECTION_UNSUPPORTED, message, region_id=region.id
        )

    @staticmethod
    def _note(code: TableFindingCode, region_id: str | None) -> TableExtractFinding:
        return finding(code, HtmlTableExtractor._NOTES[code], 0, region_id)
