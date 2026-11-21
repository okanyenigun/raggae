"""Format-neutral source handling shared by whole-source text readers."""

from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
from typing import get_args
from ..models import FindingCode, TextReadFinding, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


def require_encoding(encoding: TextEncoding) -> TextEncoding:
    if not isinstance(encoding, str):
        raise TypeError("encoding must be a string")
    if encoding not in get_args(TextEncoding):
        raise ValueError(
            "encoding must be one of: " + ", ".join(get_args(TextEncoding))
        )
    return encoding


def failure(
    reader: str, code: FindingCode, message: str, *, page_index: int | None = None
) -> TextReadResult:
    return TextReadResult(
        reader=reader,
        outcome="failed",
        findings=(
            TextReadFinding(
                code=code,
                message=message,
                page_index=page_index,
            ),
        ),
    )


def as_artifact(source: Path | str | bytes | Artifact) -> Artifact:
    if isinstance(source, bytes):
        return Artifact(data=source)
    if isinstance(source, (Path, str)):
        return Artifact(path=source)
    if not isinstance(source, Artifact):
        raise TypeError("source must be a path, bytes, or Artifact")
    return source


def select_regions(
    layout: LayoutAnalysisResult | None, region_ids: Sequence[str] | None
) -> tuple[LayoutRegion, ...] | None:
    """Validate a selection; None means no layout, all regions when IDs omitted."""
    if layout is not None and not isinstance(layout, LayoutAnalysisResult):
        raise TypeError("layout must be a LayoutAnalysisResult or None")
    if region_ids is not None:
        if layout is None:
            raise ValueError("region_ids require a layout")
        if isinstance(region_ids, (str, bytes)) or not isinstance(region_ids, Sequence):
            raise TypeError("region_ids must be a sequence of strings")
        if any(not isinstance(identifier, str) for identifier in region_ids):
            raise TypeError("region_ids must contain only strings")
        if len(set(region_ids)) != len(region_ids):
            raise ValueError("region_ids must not contain duplicates")
    if layout is None:
        return None
    if layout.outcome not in ("success", "partial"):
        raise ValueError("layout must contain a usable map")
    regions = {region.id: region for region in layout.regions}
    selected = tuple(regions) if region_ids is None else tuple(region_ids)
    if set(selected) - regions.keys():
        raise ValueError("region_ids must belong to the supplied layout")
    return tuple(regions[identifier] for identifier in selected)


def nothing_selected(reader: str) -> TextReadResult:
    """A valid request for zero regions: success, with a finding saying nothing was read."""
    return TextReadResult(
        reader=reader,
        outcome="success",
        findings=(
            TextReadFinding(
                code=FindingCode.NOTHING_SELECTED,
                message="No regions were selected, so nothing was read.",
            ),
        ),
    )


def prepare_source(
    source: Path | str | bytes | Artifact,
    *,
    reader: str,
    layout: LayoutAnalysisResult | None,
    region_ids: Sequence[str] | None,
    unsupported_message: str,
) -> Artifact | TextReadResult:
    """Resolve input and reject unsupported layout selection before source IO."""
    source = as_artifact(source)
    selected = select_regions(layout, region_ids)
    if selected is None:
        return source
    # A whole-document reader has no regions: any layout is refused, even when
    # nothing is selected (then one finding without a region).
    return TextReadResult(
        reader=reader,
        outcome="failed",
        findings=tuple(
            TextReadFinding(
                code=FindingCode.SELECTION_UNSUPPORTED,
                message=unsupported_message,
                region_id=region.id,
            )
            for region in selected
        )
        or (
            TextReadFinding(
                code=FindingCode.SELECTION_UNSUPPORTED,
                message=unsupported_message,
            ),
        ),
    )


def read_source_text(
    source: Artifact, *, encoding: TextEncoding, reader: str
) -> str | TextReadResult:
    """Read and strictly decode a regular file or in-memory source."""
    try:
        if source.path is not None and not S_ISREG(source.path.stat().st_mode):
            return failure(
                reader, FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
            )
        data = source.read_bytes()
    except OSError:
        return failure(
            reader, FindingCode.SOURCE_UNREADABLE, "Source could not be read."
        )
    try:
        return data.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return failure(
            reader,
            FindingCode.DECODE_FAILED,
            "Source could not be decoded using the configured encoding.",
        )
