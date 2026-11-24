"""Helpers shared by the table extractors: sources, findings, region selection, outcomes."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from zipfile import BadZipFile, ZipFile
from .models import (
    ExtractedTable,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from ..layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ..normalization.artifacts import Artifact
from ..shared.sources import as_artifact, select_regions


def finding(
    code: TableFindingCode,
    message: str,
    page_index: int | None = None,
    region_id: str | None = None,
) -> TableExtractFinding:
    return TableExtractFinding(
        code=code, message=message, page_index=page_index, region_id=region_id
    )


def failure(extractor: str, code: TableFindingCode, message: str) -> TableExtractResult:
    return TableExtractResult(
        extractor=extractor, outcome="failed", findings=(finding(code, message),)
    )


def prepare_source(
    source: Path | str | bytes | Artifact,
    *,
    extractor: str,
    layout: LayoutAnalysisResult | None,
    region_ids: Sequence[str] | None,
    unsupported_message: str,
) -> Artifact | TableExtractResult:
    """Resolve the source and refuse any layout before the source is opened.

    For extractors that cannot work from layout regions: one
    ``selection_unsupported`` finding per selected region, or one without a
    region when the selection is empty.
    """
    artifact = as_artifact(source)
    selected = select_regions(layout, region_ids)
    if selected is None:
        return artifact
    return TableExtractResult(
        extractor=extractor,
        outcome="failed",
        findings=tuple(
            finding(
                TableFindingCode.SELECTION_UNSUPPORTED,
                unsupported_message,
                region_id=region.id,
            )
            for region in selected
        )
        or (finding(TableFindingCode.SELECTION_UNSUPPORTED, unsupported_message),),
    )


def select_table_regions(
    layout: LayoutAnalysisResult | None, region_ids: Sequence[str] | None
) -> tuple[LayoutRegion, ...] | None:
    """None without a layout; the table regions when no IDs are given; else exactly the IDs.

    Explicit IDs are honoured whatever their kind, so a caller can ask for the
    tables inside any region.
    """
    selected = select_regions(layout, region_ids)
    if selected is not None and region_ids is None:
        selected = tuple(region for region in selected if region.kind == "table")
    return selected


def nothing_selected(extractor: str, *, explicit: bool) -> TableExtractResult:
    """A valid request for zero regions: success, with a finding saying why.

    ``explicit`` is an empty ``region_ids``; otherwise the layout had no table
    regions to select by default.
    """
    message = (
        "No regions were selected, so nothing was extracted."
        if explicit
        else "The layout has no table regions, so nothing was extracted."
    )
    return TableExtractResult(
        extractor=extractor,
        outcome="success",
        findings=(finding(TableFindingCode.NOTHING_SELECTED, message),),
    )


def read_source(
    source: Artifact, extractor: str, *, allow_empty: bool = False
) -> bytes | TableExtractResult:
    """The source's bytes, or a failed result for a missing or irregular file.

    An empty source fails too, unless ``allow_empty`` (an empty text file is a
    valid, empty document).
    """
    try:
        if source.path is not None and not S_ISREG(source.path.stat().st_mode):
            return failure(
                extractor,
                TableFindingCode.SOURCE_UNREADABLE,
                "Source must be a regular file.",
            )
        data = source.read_bytes()
    except OSError:
        return failure(
            extractor, TableFindingCode.SOURCE_UNREADABLE, "Source could not be read."
        )
    if not data and not allow_empty:
        return failure(extractor, TableFindingCode.DECODE_FAILED, "Source is empty.")
    return data


def office_package(data: bytes, extractor: str) -> TableExtractResult | None:
    """A failed result when the bytes are not an Office package; None when they are."""
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
    except BadZipFile:
        names = []
    if not names or len(set(names)) != len(names) or "[Content_Types].xml" not in names:
        return failure(
            extractor,
            TableFindingCode.DECODE_FAILED,
            "Source is not a readable Office document package.",
        )
    return None


def place_spans(
    wanted: dict[tuple[int, int], tuple[int, int]],
) -> tuple[dict[tuple[int, int], tuple[int, int]], bool]:
    """Spans for cells listed at their top-left positions, none covering another.

    ``wanted`` maps each cell's ``(row, column)`` to the ``(rows, columns)`` it
    asks to cover. Row by row, a span stops before a position where another
    cell starts or that an earlier cell already covers. Returns each cell's
    placed span, and whether any span was shortened.
    """
    covered: set[tuple[int, int]] = set()
    placed: dict[tuple[int, int], tuple[int, int]] = {}
    shortened = False
    for (row, column), (want_rows, want_columns) in sorted(wanted.items()):
        columns = 1
        while (
            columns < want_columns
            and (row, column + columns) not in wanted
            and (row, column + columns) not in covered
        ):
            columns += 1
        rows = 1
        while rows < want_rows and all(
            (row + rows, other) not in wanted and (row + rows, other) not in covered
            for other in range(column, column + columns)
        ):
            rows += 1
        shortened = shortened or rows < want_rows or columns < want_columns
        covered.update(
            (inner, other)
            for inner in range(row, row + rows)
            for other in range(column, column + columns)
        )
        placed[(row, column)] = (rows, columns)
    return placed, shortened


def finish(
    extractor: str,
    tables: list[ExtractedTable],
    findings: list[TableExtractFinding],
    completed: int,
    informational: frozenset[TableFindingCode],
) -> TableExtractResult:
    """The result: partial when a non-informational finding exists, failed with no work."""
    incomplete = any(f.code not in informational for f in findings)
    return TableExtractResult(
        extractor=extractor,
        outcome=("partial" if incomplete else "success") if completed else "failed",
        tables=tuple(tables),
        findings=tuple(findings),
    )
