"""Format-neutral source handling shared by whole-source text readers."""

from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
from typing import get_args
from ..models import FindingCode, TextReadFinding, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding
from ...sources import as_artifact, select_regions


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
