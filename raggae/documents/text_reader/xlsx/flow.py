"""Opening a workbook and the shared read flow behind the Excel readers."""

import warnings
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from .book import Book, is_worksheet, sheet_blocks
from .geometry import Box, Owned, conflicts, contains, point, range_box
from .values import CellValues
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    XlsxDrawingLocation,
    XlsxRangeLocation,
)
from ...normalization.artifacts import Artifact


type Render = Callable[
    [Book, str, int, Box, Owned, LayoutRegion | None],
    tuple[list[TextBlock], int, list[TextReadFinding]],
]


def read_workbook(
    source: Path | str | bytes | Artifact,
    *,
    layout: LayoutAnalysisResult | None,
    region_ids: Sequence[str] | None,
    reader: str,
    cell_values: CellValues,
    render: Render,
    missing_effect: str,
) -> TextReadResult:
    """Shared read flow; ``render`` turns one cell group into blocks.

    ``missing_effect`` says what happened to an unsaved formula result, e.g.
    "left empty", in that finding's message.
    """
    artifact = as_artifact(source)
    selected = select_regions(layout, region_ids)
    if selected == ():
        return nothing_selected(reader)
    try:
        if artifact.path is not None and not S_ISREG(artifact.path.stat().st_mode):
            return failure(
                reader, FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
            )
        data = artifact.read_bytes()
    except OSError:
        return failure(
            reader, FindingCode.SOURCE_UNREADABLE, "Source could not be read."
        )
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
    except BadZipFile:
        return failure(
            reader,
            FindingCode.DECODE_FAILED,
            "Source is not a readable workbook package.",
        )
    if len(set(names)) != len(names) or "[Content_Types].xml" not in names:
        return failure(
            reader,
            FindingCode.DECODE_FAILED,
            "Source is not a readable Office document package.",
        )
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as error:
        raise ImportError(
            "Excel text reading requires: pip install 'raggae[text-office]'"
        ) from error
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            formulas = load_workbook(BytesIO(data), data_only=False, keep_links=False)
            cached = (
                load_workbook(BytesIO(data), data_only=True, keep_links=False)
                if cell_values == "cached"
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
            reader,
            FindingCode.DECODE_FAILED,
            "Source could not be opened as a workbook.",
        )
    try:
        book = Book(formulas, cached)
        flow = _Flow(reader, render, missing_effect)
        result = (
            flow.standalone(book) if selected is None else flow.regions(book, selected)
        )
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
                        FindingCode.BACKEND_COVERAGE,
                        "The workbook backend reported unsupported features; "
                        "some content may be missing.",
                    ),
                )
            }
        )
    return result


class _Flow:
    def __init__(self, reader: str, render: Render, missing_effect: str) -> None:
        self.reader, self.render = reader, render
        self.missing_effect = missing_effect
        self.informational = frozenset(
            (
                FindingCode.EMPTY,
                FindingCode.HIDDEN_CONTENT,
                FindingCode.FORMULA_RESULT_MISSING,
                FindingCode.UNSUPPORTED_CONTENT,
                FindingCode.BACKEND_COVERAGE,
                FindingCode.HEADER_INVALID,
            )
        )

    def standalone(self, book: Book) -> TextReadResult:
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        for index, name in enumerate(book.formulas.sheetnames):
            sheet = book.formulas[name]
            if not is_worksheet(sheet):
                findings.append(
                    finding(
                        FindingCode.UNSUPPORTED_CONTENT,
                        "This sheet holds a chart, not cells; it was not read.",
                        page_index=index,
                    )
                )
                continue
            missing = 0
            for box, cells in sheet_blocks(sheet):
                new, gaps, notes = self.render(
                    book, name, index, box, lambda cell: cell in cells, None
                )
                blocks.extend(new)
                findings.extend(notes)
                missing += gaps
            findings.extend(self._sheet_notes(sheet, index, missing, drawings=True))
        if not blocks:
            findings.append(
                finding(
                    FindingCode.EMPTY,
                    "Workbook contains no readable cell text.",
                )
            )
        incomplete = any(f.code not in self.informational for f in findings)
        return TextReadResult(
            reader=self.reader,
            outcome=("partial" if blocks else "failed") if incomplete else "success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def regions(self, book: Book, selected: tuple[LayoutRegion, ...]) -> TextReadResult:
        names = book.formulas.sheetnames
        findings: list[TextReadFinding] = []
        resolved: list[tuple[LayoutRegion, str, Box]] = []
        for region in selected:
            location = region.location
            if isinstance(location, XlsxDrawingLocation):
                findings.append(
                    finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "Images and charts are not cell text.",
                        region=region,
                        page_index=self._position(names, location.sheet_name),
                    )
                )
                continue
            if not isinstance(location, XlsxRangeLocation):
                findings.append(
                    finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This region has no worksheet cell location.",
                        region=region,
                    )
                )
                continue
            index = self._position(names, location.sheet_name)
            if index is None:
                findings.append(
                    finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's sheet does not exist in this workbook.",
                        region=region,
                    )
                )
            elif not is_worksheet(book.formulas[location.sheet_name]):
                findings.append(
                    finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This sheet holds no cells.",
                        region=region,
                        page_index=index,
                    )
                )
            else:
                resolved.append(
                    (region, location.sheet_name, range_box(location.cell_range))
                )

        rejected = conflicts(resolved)
        blocks: list[TextBlock] = []
        completed = 0
        noted: set[str] = set()
        for region, name, box in resolved:
            index = names.index(name)
            if region.id in rejected:
                findings.append(
                    finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Another selected range shares cells with this one without "
                        "containing it; neither was read.",
                        region=region,
                        page_index=index,
                    )
                )
                continue
            completed += 1
            inner = [
                other
                for other_region, other_name, other in resolved
                if other_region.id not in rejected
                and other_region.id != region.id
                and other_name == name
                and other != box
                and contains(box, other)
            ]
            new, missing, notes = self.render(
                book,
                name,
                index,
                box,
                lambda cell: not any(contains(other, point(cell)) for other in inner),
                region,
            )
            if name not in noted:
                noted.add(name)
                findings.extend(
                    self._sheet_notes(book.formulas[name], index, 0, drawings=False)
                )
            findings.extend(notes)
            if missing:
                findings.append(self._missing(missing, index, region))
            if new:
                blocks.extend(new)
            else:
                findings.append(
                    finding(
                        FindingCode.EMPTY,
                        "Range contains no readable cell text.",
                        region=region,
                        page_index=index,
                    )
                )
        incomplete = any(f.code not in self.informational for f in findings)
        return TextReadResult(
            reader=self.reader,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _sheet_notes(
        self, sheet: Any, index: int, missing: int, *, drawings: bool
    ) -> list[TextReadFinding]:
        notes: list[TextReadFinding] = []
        if missing:
            notes.append(self._missing(missing, index))
        if (
            sheet.sheet_state != "visible"
            or any(item.hidden for item in sheet.row_dimensions.values())
            or any(item.hidden for item in sheet.column_dimensions.values())
        ):
            notes.append(
                finding(
                    FindingCode.HIDDEN_CONTENT,
                    "This sheet is hidden or has hidden rows or columns; their stored "
                    "content was read.",
                    page_index=index,
                )
            )
        if drawings and (
            sheet._images
            or sheet._charts
            or any(cell.comment is not None for cell in sheet._cells.values())
        ):
            notes.append(
                finding(
                    FindingCode.UNSUPPORTED_CONTENT,
                    "Images, charts, or cell comments on this sheet were not read.",
                    page_index=index,
                )
            )
        return notes

    def _missing(
        self, count: int, index: int, region: LayoutRegion | None = None
    ) -> TextReadFinding:
        cells = "cell has" if count == 1 else "cells have"
        return finding(
            FindingCode.FORMULA_RESULT_MISSING,
            f"{count} formula {cells} no saved result; "
            f"{self.missing_effect} rather than calculated.",
            region=region,
            page_index=index,
        )

    @staticmethod
    def _position(names: list[str], name: str) -> int | None:
        return names.index(name) if name in names else None


def finding(
    code: FindingCode,
    message: str,
    *,
    region: LayoutRegion | None = None,
    page_index: int | None = None,
) -> TextReadFinding:
    return TextReadFinding(
        code=code,
        message=message,
        region_id=None if region is None else region.id,
        page_index=page_index,
    )
