"""Tables declared on PowerPoint slides, with python-pptx."""

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
    place_spans,
    read_source,
    select_table_regions,
)
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    PptxShapeLocation,
)
from ...normalization.artifacts import Artifact
from ...shared.pptx_text import A, P, body_text, hidden, resolve
from ...shared.pptx_text import kind as shape_kind
from ...shared.pptx_text import slides as slide_list
from ...shared.sources import as_artifact

_TRUE = frozenset({"1", "true"})


class PptxTableExtractor:
    """Return the tables declared on PowerPoint slides, with python-pptx.

    Without a layout: every table shape on every slide, in slide order and, on
    a slide, in the order shapes are stored (not visual reading order); tables
    inside groups included. Each is an ``ExtractedTable`` with
    ``method="declared"`` and ``page_index`` the slide's zero-based position.
    The grid is DrawingML's: one ``a:tc`` per grid position, ``gridSpan`` and
    ``rowSpan`` on the cell that starts a merge, ``hMerge`` and ``vMerge`` on
    the positions it covers. A span reaching past the grid or over another
    cell is shortened (``span_shortened``). The table's first-row and
    first-column flags (``firstRow``, ``firstCol``) make header cells.
    ``caption`` is the table shape's alt-text title, else its alt-text
    description.

    A cell's text is its paragraphs as ``PptxTextReader`` reads them, one per
    line, without bullet symbols or numbering; blank paragraphs at its edges
    are left out. Text PowerPoint hides in a covered position is added to the
    merged cell, and a covered position no merge reaches keeps its own text.
    A table with no text in any cell is not returned (``empty_table``).
    Tables on hidden slides are read and reported (``hidden_slide``). Charts,
    SmartArt, and embedded objects are not tables.

    With a layout: the tables at or inside each requested shape, located as
    ``PptxLayoutAnalyzer`` locates them (slide part, shape path, shape ID).
    Without ``region_ids`` only table regions are used; explicit
    ``region_ids`` of any kind give the tables inside them, so a group region
    gives its tables. As in the text reader, a table inside another selected
    region belongs only to that region, and regions locating the same shape
    are both refused (``selection_overlap``). A chart or other unsupported
    shape gets ``selection_unsupported``; a shape not found,
    ``region_unresolved``; one without a table, ``empty``.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.SPAN_SHORTENED,
            TableFindingCode.HIDDEN_SLIDE,
        }
    )

    @property
    def name(self) -> str:
        return "table_extractor_pptx"

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
            from lxml.etree import LxmlError
            from pptx import Presentation
            from pptx.exc import PackageNotFoundError
        except ImportError as error:
            raise ImportError(
                "PowerPoint table extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            slides = slide_list(Presentation(BytesIO(data)))
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be opened as a presentation.",
            )
        try:
            if selected is None:
                return self._standalone(slides)
            return self._regions(slides, selected)
        except RecursionError:
            return failure(
                self.name,
                TableFindingCode.STRUCTURE_UNSUPPORTED,
                "Shape groups are nested too deeply to read within Python's "
                "recursion limit.",
            )

    def _standalone(self, slides: list[Any]) -> TableExtractResult:
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        any_table = False
        for index, slide in enumerate(slides):
            found = PptxTableExtractor._tables(slide.shapes, frozenset())
            any_table = any_table or bool(found)
            if found and hidden(slide):
                findings.append(PptxTableExtractor._hidden(index))
            for shape in found:
                PptxTableExtractor._read(shape, index, None, tables, findings)
        if not any_table:
            findings.append(finding(TableFindingCode.EMPTY, "No tables on the slides."))
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, slides: list[Any], selected: tuple[LayoutRegion, ...]
    ) -> TableExtractResult:
        by_part = {
            str(slide.part.partname).lstrip("/"): (index, slide)
            for index, slide in enumerate(slides)
        }
        findings: list[TableExtractFinding] = []
        resolved: list[tuple[LayoutRegion, int, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, PptxShapeLocation):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "Selection is not a slide shape.",
                        region_id=region.id,
                    )
                )
                continue
            found = by_part.get(location.part)
            shape = None if found is None else resolve(found[1], location.shape_path)
            if found is None or shape is None or shape.shape_id != location.shape_id:
                findings.append(
                    finding(
                        TableFindingCode.REGION_UNRESOLVED,
                        "The region's shape was not found in this presentation.",
                        None if found is None else found[0],
                        region.id,
                    )
                )
                continue
            if shape_kind(shape) == "unsupported":
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "Charts, SmartArt, and embedded objects are not tables.",
                        found[0],
                        region.id,
                    )
                )
                continue
            resolved.append((region, found[0], shape))

        counts: dict[int, int] = {}
        for _, _, shape in resolved:
            counts[id(shape.element)] = counts.get(id(shape.element), 0) + 1
        owners = frozenset(id(shape.element) for _, _, shape in resolved)
        hidden_reported: set[int] = set()
        tables: list[ExtractedTable] = []
        completed = 0
        for region, index, shape in resolved:
            if counts[id(shape.element)] > 1:
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_OVERLAP,
                        "Selected regions locate the same shape; neither was extracted.",
                        index,
                        region.id,
                    )
                )
                continue
            completed += 1
            excluded = owners - {id(shape.element)}
            found = PptxTableExtractor._tables_at(shape, excluded)
            if not found:
                findings.append(
                    finding(
                        TableFindingCode.EMPTY,
                        "No table in this region.",
                        index,
                        region.id,
                    )
                )
                continue
            if index not in hidden_reported and hidden(slides[index]):
                hidden_reported.add(index)
                findings.append(PptxTableExtractor._hidden(index))
            for table in found:
                PptxTableExtractor._read(table, index, region.id, tables, findings)
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _tables(shapes: Any, excluded: frozenset[int]) -> list[Any]:
        """Table shapes in storage order, looking into groups."""
        found: list[Any] = []
        for shape in shapes:
            if id(shape.element) in excluded:
                continue
            kind = shape_kind(shape)
            if kind == "group":
                found.extend(PptxTableExtractor._tables(shape.shapes, excluded))
            elif kind == "table":
                found.append(shape)
        return found

    @staticmethod
    def _tables_at(shape: Any, excluded: frozenset[int]) -> list[Any]:
        """The table shape itself, or the tables inside a group."""
        kind = shape_kind(shape)
        if kind == "table":
            return [shape]
        if kind == "group":
            return PptxTableExtractor._tables(shape.shapes, excluded)
        return []

    @staticmethod
    def _read(
        shape: Any,
        page_index: int,
        region_id: str | None,
        tables: list[ExtractedTable],
        findings: list[TableExtractFinding],
    ) -> None:
        table, shortened = PptxTableExtractor._grid(shape, page_index, region_id)
        if shortened:
            findings.append(
                finding(
                    TableFindingCode.SPAN_SHORTENED,
                    "A cell's span ran past the table or into another cell and was "
                    "shortened.",
                    page_index,
                    region_id,
                )
            )
        if table is None:
            findings.append(
                finding(
                    TableFindingCode.EMPTY_TABLE,
                    "A table with no text in any cell was not returned.",
                    page_index,
                    region_id,
                )
            )
        else:
            tables.append(table)

    @staticmethod
    def _grid(
        shape: Any, page_index: int, region_id: str | None
    ) -> tuple[ExtractedTable | None, bool]:
        """The table's grid; None when no cell has text, and whether a span was cut."""
        table = shape.table._tbl
        rows = table.findall(f"{A}tr")
        grid = table.find(f"{A}tblGrid")
        declared = 0 if grid is None else len(grid.findall(f"{A}gridCol"))
        row_count = len(rows)
        column_count = max(
            [declared, *(len(row.findall(f"{A}tc")) for row in rows)], default=0
        )
        starts: dict[tuple[int, int], str] = {}
        covered_texts: list[tuple[int, int, str]] = []
        wanted: dict[tuple[int, int], tuple[int, int]] = {}
        clipped = False
        for y, row in enumerate(rows):
            for x, cell in enumerate(row.findall(f"{A}tc")):
                body = cell.find(f"{A}txBody")
                text = (None if body is None else body_text(body)) or ""
                if cell.get("hMerge") in _TRUE or cell.get("vMerge") in _TRUE:
                    if text.strip():
                        covered_texts.append((y, x, text))
                    continue
                want_rows = PptxTableExtractor._span(cell.get("rowSpan"))
                want_columns = PptxTableExtractor._span(cell.get("gridSpan"))
                fitted = (
                    min(want_rows, row_count - y),
                    min(want_columns, column_count - x),
                )
                clipped = clipped or fitted != (want_rows, want_columns)
                wanted[(y, x)] = fitted
                starts[(y, x)] = text
        spans, shortened = place_spans(wanted)
        owner = {
            (inner, other): start
            for start, (rows_spanned, columns_spanned) in spans.items()
            for inner in range(start[0], start[0] + rows_spanned)
            for other in range(start[1], start[1] + columns_spanned)
        }
        texts = dict(starts)
        for y, x, text in covered_texts:
            start = owner.get((y, x))
            if start is None:
                spans[(y, x)] = (1, 1)  # A covered position no merge reaches.
                texts[(y, x)] = text
            else:
                texts[start] = f"{texts[start]}\n{text}" if texts[start] else text
        if not any(text.strip() for text in texts.values()):
            return None, shortened or clipped
        properties = table.find(f"{A}tblPr")
        first_row = properties is not None and properties.get("firstRow") in _TRUE
        first_column = properties is not None and properties.get("firstCol") in _TRUE
        cells = tuple(
            TableCell(
                row=y,
                column=x,
                row_span=spans[(y, x)][0],
                column_span=spans[(y, x)][1],
                text=texts[(y, x)],
                header=(first_row and y == 0) or (first_column and x == 0),
            )
            for y, x in sorted(spans)
        )
        return (
            ExtractedTable(
                cells=cells,
                row_count=row_count,
                column_count=column_count,
                page_index=page_index,
                region_id=region_id,
                method="declared",
                caption=PptxTableExtractor._caption(shape),
            ),
            shortened or clipped,
        )

    @staticmethod
    def _span(value: str | None) -> int:
        """A gridSpan or rowSpan; 1 when absent or invalid."""
        digits = (value or "").strip()
        if not digits.isdigit():
            return 1
        return max(1, int(digits[:6]))

    @staticmethod
    def _caption(shape: Any) -> str | None:
        names = shape.element.findall(f"{P}nvGraphicFramePr/{P}cNvPr")
        if not names:
            return None
        return names[0].get("title") or names[0].get("descr") or None

    @staticmethod
    def _hidden(page_index: int) -> TableExtractFinding:
        return finding(
            TableFindingCode.HIDDEN_SLIDE,
            "This slide is hidden in slide shows; its tables were read.",
            page_index,
        )
