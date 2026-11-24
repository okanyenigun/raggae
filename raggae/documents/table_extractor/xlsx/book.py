"""Tables declared in an Excel workbook, and worksheet ranges, with openpyxl."""

import warnings
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
    XlsxDrawingLocation,
    XlsxRangeLocation,
)
from ...normalization.artifacts import Artifact
from ...shared.sources import as_artifact
from ...shared.xlsx_text import (
    MISSING,
    Box,
    CellValues,
    Owned,
    conflicts,
    contains,
    is_worksheet,
    point,
    range_box,
    require_cell_values,
    stored_value,
    value_text,
)


class XlsxTableExtractor:
    """Return the tables declared in an Excel workbook, or worksheet ranges, with openpyxl.

    Without a layout: every declared Excel table (a ``ListObject``) on every
    worksheet, in sheet order. Cells outside declared tables are not read;
    each sheet holding some gets one informational ``cells_outside_tables``.
    Each table is an ``ExtractedTable`` with ``method="declared"`` and
    ``page_index`` the sheet's zero-based position (chart sheets counted). Its
    grid is the table's range, empty cells included; the table's header rows
    (``headerRowCount``, usually 1) hold header cells. ``caption`` is None: a
    table's name (``Table1``) is a name, not a caption.

    A cell's value is written as ``XlsxTextReader`` writes it: stored values,
    not Excel's display formats; numbers as stored, dates in ISO 8601,
    ``TRUE``/``FALSE``; line breaks kept. By default a formula cell gives its
    saved result (``cell_values="cached"``); ``cell_values="formulas"`` gives
    the formula text. Formulas are never evaluated: a formula without a saved
    result is left empty and reported (``formula_result_missing``). A merged
    range is one cell with its spans; a merge reaching past the range is cut at
    its edge (``span_shortened``), and positions merged from outside the range
    are not covered. A table with no text is not returned (``empty_table``).
    Hidden sheets, rows, and columns are read and reported (``hidden_content``).

    With a layout: each requested range is read as one grid, so a range the
    analyzer marks ``unknown`` (a block of filled cells) gives a table when
    asked for by ID. Without ``region_ids`` only table regions (the declared
    tables) are used. Cells in a declared table's header rows are header cells
    in any range. As in the text readers, a range inside another selected
    range owns its cells (left empty in the outer one), and ranges that cross
    are both refused (``selection_overlap``). Images and charts get
    ``selection_unsupported``; a sheet not in the workbook,
    ``region_unresolved``.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.SPAN_SHORTENED,
            TableFindingCode.HIDDEN_CONTENT,
            TableFindingCode.FORMULA_RESULT_MISSING,
            TableFindingCode.BACKEND_COVERAGE,
            TableFindingCode.CELLS_OUTSIDE_TABLES,
        }
    )

    def __init__(self, *, cell_values: CellValues = "cached") -> None:
        self._cell_values: CellValues = require_cell_values(cell_values)

    @property
    def name(self) -> str:
        return "table_extractor_xlsx"

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
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError as error:
            raise ImportError(
                "Excel table extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                formulas = load_workbook(
                    BytesIO(data), data_only=False, keep_links=False
                )
                cached = (
                    load_workbook(BytesIO(data), data_only=True, keep_links=False)
                    if self._cell_values == "cached"
                    else None
                )
        except (
            BadZipFile,
            InvalidFileException,
            SyntaxError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OSError,
        ):
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be opened as a workbook.",
            )
        try:
            if selected is None:
                result = self._standalone(formulas, cached)
            else:
                result = self._regions(formulas, cached, selected)
        finally:
            formulas.close()
            if cached is not None:
                cached.close()
        if any(issubclass(item.category, UserWarning) for item in captured):
            result = result.model_copy(
                update={
                    "findings": result.findings
                    + (
                        finding(
                            TableFindingCode.BACKEND_COVERAGE,
                            "The workbook backend reported unsupported features; "
                            "some content may be missing.",
                        ),
                    )
                }
            )
        return result

    def _standalone(self, formulas: Any, cached: Any | None) -> TableExtractResult:
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        declared_anywhere = False
        for index, name in enumerate(formulas.sheetnames):
            sheet = formulas[name]
            if not is_worksheet(sheet):
                continue
            saved = None if cached is None else cached[name]._cells
            headers = XlsxTableExtractor._header_cells(sheet)
            boxes: list[Box] = []
            missing = 0
            for declared in sheet.tables.values():
                declared_anywhere = True
                box = range_box(declared.ref)
                boxes.append(box)
                table, gaps, shortened = XlsxTableExtractor._grid(
                    sheet, saved, box, lambda _: True, headers, index, None
                )
                missing += gaps
                XlsxTableExtractor._collect(
                    table, shortened, index, None, tables, findings
                )
            if missing:
                findings.append(XlsxTableExtractor._missing(missing, index))
            if boxes and XlsxTableExtractor._has_hidden(sheet):
                findings.append(XlsxTableExtractor._hidden(index))
            if any(
                cell.value is not None
                and not any(contains(box, point(position)) for box in boxes)
                for position, cell in sheet._cells.items()
            ):
                findings.append(
                    finding(
                        TableFindingCode.CELLS_OUTSIDE_TABLES,
                        "This sheet holds filled cells outside declared tables; they "
                        "were not read. Select their ranges with a layout to read them.",
                        index,
                    )
                )
        if not declared_anywhere:
            findings.insert(
                0,
                finding(TableFindingCode.EMPTY, "No declared tables in the workbook."),
            )
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, formulas: Any, cached: Any | None, selected: tuple[LayoutRegion, ...]
    ) -> TableExtractResult:
        names = formulas.sheetnames
        findings: list[TableExtractFinding] = []
        resolved: list[tuple[LayoutRegion, str, Box]] = []
        for region in selected:
            location = region.location
            if isinstance(location, XlsxDrawingLocation):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "Images and charts are not tables.",
                        XlsxTableExtractor._position(names, location.sheet_name),
                        region.id,
                    )
                )
                continue
            if not isinstance(location, XlsxRangeLocation):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "Selection is not a worksheet range.",
                        region_id=region.id,
                    )
                )
                continue
            index = XlsxTableExtractor._position(names, location.sheet_name)
            if index is None:
                findings.append(
                    finding(
                        TableFindingCode.REGION_UNRESOLVED,
                        "The region's sheet does not exist in this workbook.",
                        region_id=region.id,
                    )
                )
            elif not is_worksheet(formulas[location.sheet_name]):
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_UNSUPPORTED,
                        "This sheet holds no cells.",
                        index,
                        region.id,
                    )
                )
            else:
                resolved.append(
                    (region, location.sheet_name, range_box(location.cell_range))
                )

        rejected = conflicts(resolved)
        tables: list[ExtractedTable] = []
        completed = 0
        noted: set[str] = set()
        for region, name, box in resolved:
            index = names.index(name)
            if region.id in rejected:
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_OVERLAP,
                        "Another selected range shares cells with this one without "
                        "containing it; neither was extracted.",
                        index,
                        region.id,
                    )
                )
                continue
            completed += 1
            sheet = formulas[name]
            if name not in noted:
                noted.add(name)
                if XlsxTableExtractor._has_hidden(sheet):
                    findings.append(XlsxTableExtractor._hidden(index))
            inner = [
                other
                for other_region, other_name, other in resolved
                if other_region.id not in rejected
                and other_region.id != region.id
                and other_name == name
                and other != box
                and contains(box, other)
            ]
            table, missing, shortened = XlsxTableExtractor._grid(
                sheet,
                None if cached is None else cached[name]._cells,
                box,
                lambda cell: not any(contains(other, point(cell)) for other in inner),
                XlsxTableExtractor._header_cells(sheet),
                index,
                region.id,
            )
            if missing:
                findings.append(XlsxTableExtractor._missing(missing, index, region.id))
            XlsxTableExtractor._collect(
                table, shortened, index, region.id, tables, findings
            )
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _grid(
        sheet: Any,
        saved: dict[tuple[int, int], Any] | None,
        box: Box,
        owned: Owned,
        headers: set[tuple[int, int]],
        page_index: int,
        region_id: str | None,
    ) -> tuple[ExtractedTable | None, int, bool]:
        """The range as a grid; None when no cell has text. Also the count of
        formulas without a saved result, and whether a merge was cut."""
        left, top, right, bottom = box
        wanted: dict[tuple[int, int], tuple[int, int]] = {}
        merged: set[tuple[int, int]] = set()
        clipped = False
        for area in sheet.merged_cells.ranges:
            if (
                area.max_col < left
                or area.min_col > right
                or area.max_row < top
                or area.min_row > bottom
            ):
                continue
            origin = (area.min_row, area.min_col)
            inside = contains(box, point(origin))
            for row in range(max(area.min_row, top), min(area.max_row, bottom) + 1):
                for column in range(
                    max(area.min_col, left), min(area.max_col, right) + 1
                ):
                    if (row, column) != origin:
                        merged.add((row, column))
            if inside:
                rows = min(area.max_row, bottom) - area.min_row + 1
                columns = min(area.max_col, right) - area.min_col + 1
                clipped = (
                    clipped
                    or rows <= area.max_row - area.min_row
                    or (columns <= area.max_col - area.min_col)
                )
                wanted[(area.min_row - top, area.min_col - left)] = (rows, columns)
        for row in range(top, bottom + 1):
            for column in range(left, right + 1):
                position = (row - top, column - left)
                if (row, column) not in merged and position not in wanted:
                    wanted[position] = (1, 1)
        spans, shortened = place_spans(wanted)
        cells: list[TableCell] = []
        missing = 0
        for (row, column), (rows, columns) in sorted(spans.items()):
            position = (row + top, column + left)
            value = (
                stored_value(sheet._cells.get(position), saved)
                if owned(position)
                else None
            )
            if value is MISSING:
                missing += 1
                value = None
            cells.append(
                TableCell(
                    row=row,
                    column=column,
                    row_span=rows,
                    column_span=columns,
                    text=value_text(value),
                    header=position in headers,
                )
            )
        if not any(cell.text.strip() for cell in cells):
            return None, missing, shortened or clipped
        return (
            ExtractedTable(
                cells=tuple(cells),
                row_count=bottom - top + 1,
                column_count=right - left + 1,
                page_index=page_index,
                region_id=region_id,
                method="declared",
            ),
            missing,
            shortened or clipped,
        )

    @staticmethod
    def _header_cells(sheet: Any) -> set[tuple[int, int]]:
        """The (row, column) positions in the header rows of the sheet's declared tables."""
        positions: set[tuple[int, int]] = set()
        for declared in sheet.tables.values():
            left, top, right, bottom = range_box(declared.ref)
            count = declared.headerRowCount
            count = 1 if count is None else int(count)
            for row in range(top, min(top + count, bottom + 1)):
                positions.update((row, column) for column in range(left, right + 1))
        return positions

    @staticmethod
    def _collect(
        table: ExtractedTable | None,
        shortened: bool,
        page_index: int,
        region_id: str | None,
        tables: list[ExtractedTable],
        findings: list[TableExtractFinding],
    ) -> None:
        if shortened:
            findings.append(
                finding(
                    TableFindingCode.SPAN_SHORTENED,
                    "A merged range reaches past the range read; it was cut at its edge.",
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
    def _has_hidden(sheet: Any) -> bool:
        return (
            sheet.sheet_state != "visible"
            or any(item.hidden for item in sheet.row_dimensions.values())
            or any(item.hidden for item in sheet.column_dimensions.values())
        )

    @staticmethod
    def _hidden(page_index: int) -> TableExtractFinding:
        return finding(
            TableFindingCode.HIDDEN_CONTENT,
            "This sheet is hidden or has hidden rows or columns; their stored content "
            "was read.",
            page_index,
        )

    @staticmethod
    def _missing(
        count: int, page_index: int, region_id: str | None = None
    ) -> TableExtractFinding:
        cells = "cell has" if count == 1 else "cells have"
        return finding(
            TableFindingCode.FORMULA_RESULT_MISSING,
            f"{count} formula {cells} no saved result; left empty rather than "
            "calculated.",
            page_index,
            region_id,
        )

    @staticmethod
    def _position(names: list[str], name: str) -> int | None:
        return names.index(name) if name in names else None
