"""Tables pdfplumber finds on PDF pages, from drawn lines or from text alignment."""

from bisect import bisect_left
from collections.abc import Sequence
from io import BytesIO
from math import isclose
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractMethod,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import (
    failure,
    finding,
    finish,
    nothing_selected,
    place_spans,
    read_source,
    select_table_regions,
)
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from ...layout_analysis.region_discovery.pdf.geometry import (
    contains_center,
    displayed_crop,
)
from ...normalization.artifacts import Artifact
from ...shared.pdf_geometry import unrotated_characters
from ...shared.sources import as_artifact

if TYPE_CHECKING:
    from pdfplumber.page import Page

type PdfplumberStrategy = Literal["all", "lines", "text"]


class PdfplumberTableExtractor:
    """Return the tables pdfplumber finds on PDF pages, as inferred grids.

    ``strategy="lines"`` builds each grid from drawn lines and rectangle
    edges: ``method="ruled"``. ``strategy="text"`` builds it from where the
    words sit: ``method="aligned"``. ``strategy="all"`` does both: first the
    ruled tables, then the aligned ones among the characters no ruled table
    holds, so no text is in two tables; the tables come top to bottom, each
    with its own ``method``. All are pdfplumber's ``find_tables()`` with its
    default settings, so a grid is a hypothesis: the lines strategy misses
    tables drawn without borders, and the text strategy also turns ordinary
    text into rows and cuts words at cell edges. The text strategy makes each
    gap between text lines a row; rows with no text are dropped from
    ``aligned`` grids, while ``ruled`` grids keep their empty rows, which were
    drawn.

    The default (``strategy=None``) depends on the layout. With a layout it
    is ``"all"``: only the selected regions are searched, so each table
    region is read the way it is drawn. Without one it is ``"lines"``: on a
    whole page the text strategy turns ordinary paragraphs into tables, so
    it runs only when asked for.

    Without a layout: the tables found on each page's visible area, in
    pdfplumber's order (top to bottom), with the page's zero-based
    ``page_index``. A cell that spans several of the grid's row or column
    bands gets that ``row_span`` or ``column_span``; a span that would cover
    another cell is shortened (``span_shortened``). No cell is a header
    (``header=False``) and no table has a caption: the PDF declares neither.
    A cell's text is the stored characters whose centers lie in it,
    assembled as ``PdfplumberTextReader`` assembles them; no OCR. A grid with
    no text in any cell is not returned (``empty_table``); a document without
    tables gets ``empty``.

    With a layout: tables are searched inside each requested rectangle, in
    the coordinates ``NativePdfLayoutAnalyzer`` and ``VisualLayoutAnalyzer``
    use (displayed crop, top-left origin, points, rotation and UserUnit
    applied). Each table found there gets the region's ID; none gives
    ``empty``. Without ``region_ids`` only table regions are used; explicit
    ``region_ids`` of any kind are searched. As in the text reader, regions
    sharing characters are both refused (``selection_overlap``); a region
    that is not on a PDF page gets ``selection_unsupported``, one on a page
    of another size ``layout_mismatch``, one past the last page
    ``page_missing``.

    Passwords come from the ``Artifact``. A page that cannot be read gets
    ``page_unreadable`` and the other pages are kept.
    """

    # Cell edges come from the same snapped intersections, so they match exactly;
    # the tolerance only absorbs floating-point noise.
    _EDGE_TOLERANCE = 1e-3

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.SPAN_SHORTENED,
        }
    )

    def __init__(self, *, strategy: PdfplumberStrategy | None = None) -> None:
        if strategy is not None:
            if not isinstance(strategy, str):
                raise TypeError("strategy must be a string or None")
            if strategy not in ("all", "lines", "text"):
                raise ValueError("strategy must be 'all', 'lines', or 'text'")
        self._strategy: PdfplumberStrategy | None = strategy

    @property
    def name(self) -> str:
        return "table_extractor_pdfplumber"

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
        if b"%PDF-" not in data[:1024]:
            return TableExtractResult(
                extractor=self.name,
                outcome="not_applicable",
                findings=(
                    finding(
                        TableFindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
        return self._extract_pdf(data, artifact.password, surfaces, selected)

    def _extract_pdf(
        self,
        data: bytes,
        password: str | None,
        surfaces: dict[str, LayoutSurface],
        selected: tuple[LayoutRegion, ...] | None,
    ) -> TableExtractResult:
        try:
            import pdfplumber
            from pdfminer.pdfdocument import PDFPasswordIncorrect
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdftypes import PDFException
            from pdfminer.psparser import PSException
            from pdfplumber.page import Page
            from pdfplumber.utils.exceptions import (
                MalformedPDFException,
                PdfminerException,
            )
        except ImportError as error:
            raise ImportError(
                "PDF table extraction requires: pip install 'raggae[text-pdf]'"
            ) from error
        parse_errors = (
            PDFException,
            PSException,
            PdfminerException,
            MalformedPDFException,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            RecursionError,
            OSError,
        )
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                location = surfaces[region.surface_id].location
                if not isinstance(location, PdfPageLocation):
                    findings.append(
                        finding(
                            TableFindingCode.SELECTION_UNSUPPORTED,
                            "Selection is not a region on a PDF page.",
                            region_id=region.id,
                        )
                    )
                    continue
                pending.setdefault(location.page_number, []).append(region)
            if not pending:
                return finish(self.name, [], findings, 0, self._INFORMATIONAL)

        pending_code = TableFindingCode.PAGE_MISSING
        pending_message = "Requested page does not exist in this PDF."
        with BytesIO(data) as stream:
            try:
                document = pdfplumber.open(
                    stream, password=password, strict_metadata=True
                )
            except parse_errors as error:
                cause = (
                    error.args[0]
                    if isinstance(error, PdfminerException) and error.args
                    else error
                )
                if isinstance(cause, PDFPasswordIncorrect):
                    code = (
                        TableFindingCode.INVALID_PASSWORD
                        if password
                        else TableFindingCode.PASSWORD_REQUIRED
                    )
                    return failure(
                        self.name,
                        code,
                        "PDF could not be opened with the supplied password.",
                    )
                return failure(
                    self.name,
                    TableFindingCode.DECODE_FAILED,
                    "PDF could not be opened.",
                )
            try:
                # Parse only requested pages and release each page promptly;
                # PDF.pages would materialize every page.
                for number, raw_page in enumerate(
                    PDFPage.create_pages(document.doc), 1
                ):
                    if selected is not None and number not in pending:
                        continue
                    regions = pending.pop(number) if selected is not None else None
                    page = None
                    try:
                        page = Page(document, raw_page, page_number=number)
                        page_tables, page_findings, page_completed = self._page(
                            page, regions, surfaces
                        )
                    except parse_errors:
                        for region in regions if regions is not None else (None,):
                            findings.append(
                                finding(
                                    TableFindingCode.PAGE_UNREADABLE,
                                    "Tables on this PDF page could not be read.",
                                    number - 1,
                                    None if region is None else region.id,
                                )
                            )
                    else:
                        tables.extend(page_tables)
                        findings.extend(page_findings)
                        completed += page_completed
                    finally:
                        if page is not None:
                            page.close()
                    if selected is not None and not pending:
                        break
            except parse_errors:
                pending_code = TableFindingCode.DECODE_FAILED
                pending_message = "PDF page traversal could not finish."
                findings.append(finding(pending_code, pending_message))
            finally:
                document.flush_cache()

        for number, regions in pending.items():
            for region in regions:
                findings.append(
                    finding(pending_code, pending_message, number - 1, region.id)
                )
        if not completed and not findings:
            findings.append(
                finding(TableFindingCode.NO_PAGES, "PDF contains no usable pages.")
            )
        elif selected is None and completed and not tables and not findings:
            findings.append(
                finding(TableFindingCode.EMPTY, "No tables were found in the document.")
            )
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    def _page(
        self,
        page: "Page",
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> tuple[list[ExtractedTable], list[TableExtractFinding], int]:
        crop, unit = displayed_crop(page)
        width, height = (crop[2] - crop[0]) * unit, (crop[3] - crop[1]) * unit
        page_index = page.page_number - 1
        findings: list[TableExtractFinding] = []
        scopes: list[tuple[str | None, tuple[float, float, float, float]]] = []
        if regions is None:
            scopes.append((None, crop))
        else:
            for region in regions:
                surface = surfaces[region.surface_id]
                if (
                    region.bbox is None
                    or surface.width is None
                    or surface.height is None
                    or not isclose(surface.width, width, abs_tol=0.01, rel_tol=1e-6)
                    or not isclose(surface.height, height, abs_tol=0.01, rel_tol=1e-6)
                ):
                    findings.append(
                        finding(
                            TableFindingCode.LAYOUT_MISMATCH,
                            "Layout dimensions do not match the source page.",
                            page_index,
                            region.id,
                        )
                    )
                    continue
                left, top, right, bottom = region.bbox
                scopes.append(
                    (
                        region.id,
                        (
                            max(crop[0], crop[0] + left / unit),
                            max(crop[1], crop[1] + top / unit),
                            min(crop[2], crop[0] + right / unit),
                            min(crop[3], crop[1] + bottom / unit),
                        ),
                    )
                )
        if not scopes:
            return [], findings, 0

        # Each visible character belongs to the one scope holding its center;
        # scopes sharing a character are refused, as in the text reader.
        assignments: list[list[dict[str, Any]]] = [[] for _ in scopes]
        conflicts: set[int] = set()
        for char in page.chars:
            box = PdfplumberTableExtractor._box(char)
            if not contains_center(crop, box):
                continue
            owners = [
                index
                for index, (_, area) in enumerate(scopes)
                if contains_center(area, box)
            ]
            if len(owners) > 1:
                conflicts.update(owners)
            elif owners:
                assignments[owners[0]].append(char)

        # The default searches regions both ways, whole pages only for lines.
        strategy = self._strategy or ("lines" if regions is None else "all")
        tables: list[ExtractedTable] = []
        completed = 0
        for index, (region_id, area) in enumerate(scopes):
            if index in conflicts:
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_OVERLAP,
                        "Selected regions share source characters; neither was extracted.",
                        page_index,
                        region_id,
                    )
                )
                continue
            completed += 1
            found = PdfplumberTableExtractor._find(
                page.crop(area), assignments[index], strategy
            )
            if not found and region_id is not None:
                findings.append(
                    finding(
                        TableFindingCode.EMPTY,
                        "No table was found in this area.",
                        page_index,
                        region_id,
                    )
                )
            shortened = False
            for backend_table, chars, method in found:
                cells, rows, columns, cut = PdfplumberTableExtractor._grid(
                    backend_table.rows, chars, page.rotation
                )
                shortened = shortened or cut
                if not any(cell.text.strip() for cell in cells):
                    findings.append(
                        finding(
                            TableFindingCode.EMPTY_TABLE,
                            "A table with no text in any cell was not returned.",
                            page_index,
                            region_id,
                        )
                    )
                    continue
                if method == "aligned":
                    cells, rows = PdfplumberTableExtractor._text_rows(cells, rows)
                tables.append(
                    ExtractedTable(
                        cells=tuple(cells),
                        row_count=rows,
                        column_count=columns,
                        page_index=page_index,
                        region_id=region_id,
                        method=method,
                    )
                )
            if shortened:
                findings.append(
                    finding(
                        TableFindingCode.SPAN_SHORTENED,
                        "A cell's span ran into another cell and was shortened.",
                        page_index,
                        region_id,
                    )
                )
        return tables, findings, completed

    @staticmethod
    def _find(
        area: Any, chars: list[dict[str, Any]], strategy: PdfplumberStrategy
    ) -> list[tuple[Any, list[dict[str, Any]], TableExtractMethod]]:
        """The tables found in an area, top to bottom, each with the characters
        its cells may hold and its method.

        With ``strategy="all"``, the text strategy runs only on the characters
        no ruled table holds, and its tables take their text only from them.
        """
        found: list[tuple[Any, list[dict[str, Any]], TableExtractMethod]] = []
        if strategy in ("all", "lines"):
            for table in area.find_tables(
                {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
            ):
                found.append((table, chars, "ruled"))
        if strategy in ("all", "text"):
            ruled = [table.bbox for table, _, _ in found]

            def free(item: dict[str, Any]) -> bool:
                box = PdfplumberTableExtractor._box(item)
                return not any(contains_center(bbox, box) for bbox in ruled)

            searched = (
                area.filter(
                    lambda item: item.get("object_type") != "char" or free(item)
                )
                if ruled
                else area
            )
            rest = [char for char in chars if free(char)]
            for table in searched.find_tables(
                {"vertical_strategy": "text", "horizontal_strategy": "text"}
            ):
                found.append((table, rest, "aligned"))
        found.sort(key=lambda item: (item[0].bbox[1], item[0].bbox[0]))
        return found

    @staticmethod
    def _grid(
        rows: Sequence[Any], chars: list[dict[str, Any]], rotation: int
    ) -> tuple[list[TableCell], int, int, bool]:
        """Cells from pdfplumber's rows, spans measured against its bands.

        Each row lists, per column band, the cell box starting there or None.
        A box reaching over further bands spans them; a span stops before any
        position another cell starts at or already covers.
        """
        from pdfplumber.utils.text import extract_text

        starts = {
            (r, c): box
            for r, row in enumerate(rows)
            for c, box in enumerate(row.cells)
            if box is not None
        }
        boxes = list(starts.values())
        tolerance = PdfplumberTableExtractor._EDGE_TOLERANCE
        tops = sorted({box[1] for box in boxes})
        lefts = sorted({box[0] for box in boxes})
        spans, shortened = place_spans(
            {
                (r, c): (
                    bisect_left(tops, box[3] - tolerance) - r,
                    bisect_left(lefts, box[2] - tolerance) - c,
                )
                for (r, c), box in starts.items()
            }
        )
        cells: list[TableCell] = []
        for (r, c), box in sorted(starts.items()):
            inside = [
                char
                for char in chars
                if box[0] <= (char["x0"] + char["x1"]) / 2 < box[2]
                and box[1] <= (char["top"] + char["bottom"]) / 2 < box[3]
            ]
            text = extract_text(unrotated_characters(inside, rotation))
            row_span, column_span = spans[(r, c)]
            cells.append(
                TableCell(
                    row=r,
                    column=c,
                    row_span=row_span,
                    column_span=column_span,
                    text=text,
                )
            )
        return cells, len(rows), len(lefts), shortened

    @staticmethod
    def _text_rows(
        cells: list[TableCell], row_count: int
    ) -> tuple[list[TableCell], int]:
        """The grid without its rows where no cell with text starts, renumbered.

        Cells starting in a dropped row hold no text and go with it; a cell
        spanning a dropped row spans one row fewer.
        """
        kept = sorted({cell.row for cell in cells if cell.text.strip()})
        number = {row: index for index, row in enumerate(kept)}
        return [
            cell.model_copy(
                update={
                    "row": number[cell.row],
                    "row_span": sum(
                        row in number
                        for row in range(cell.row, cell.row + cell.row_span)
                    ),
                }
            )
            for cell in cells
            if cell.row in number
        ], len(kept)

    @staticmethod
    def _box(char: dict[str, Any]) -> tuple[float, float, float, float]:
        return (
            float(char["x0"]),
            float(char["top"]),
            float(char["x1"]),
            float(char["bottom"]),
        )
