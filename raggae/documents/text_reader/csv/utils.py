"""CSV findings shared by the CSV reader family.

The record framing moved to ``raggae.documents.shared.csv_text``, shared with the CSV
table extractor; it is imported back here so this import path keeps working.
"""

from collections.abc import Sequence
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ...shared.csv_text import (
    CsvParseError,
    CsvRow,
    column_label,
    parsed_rows,
    require_delimiter,
    require_header,
    unique_names,
)

__all__ = [
    "CsvParseError",
    "CsvRow",
    "column_label",
    "empty_finding",
    "mismatch_finding",
    "parse_finding",
    "parsed_rows",
    "require_delimiter",
    "require_header",
    "stopped",
    "unique_names",
]


def _fields(count: int) -> str:
    return f"{count} field" if count == 1 else f"{count} fields"


def parse_finding(
    error: CsvParseError,
) -> TextReadFinding:
    if error.field_too_large:
        return TextReadFinding(
            code=FindingCode.FIELD_TOO_LARGE,
            message=(
                f"Row starting on line {error.line} has a field larger than Python's "
                "csv field size limit; it and later rows were not returned."
            ),
            page_index=0,
        )
    return TextReadFinding(
        code=FindingCode.PARSE_FAILED,
        message=(
            f"Row starting on line {error.line} could not be parsed as CSV; "
            "it and later rows were not returned."
        ),
        page_index=0,
    )


def mismatch_finding(
    row: CsvRow,
    header_count: int,
) -> TextReadFinding:
    """A misaligned row may shift values, and later rows', into wrong columns."""
    return TextReadFinding(
        code=FindingCode.ROW_LENGTH_MISMATCH,
        message=(
            f"Row starting on line {row.line} has {_fields(len(row.fields))}; "
            f"the header has {_fields(header_count)}; it and later rows were "
            "not returned."
        ),
        page_index=0,
    )


def empty_finding(*, header_seen: bool) -> TextReadFinding:
    return TextReadFinding(
        code=FindingCode.EMPTY,
        message=(
            "Source contains a header but no data rows."
            if header_seen
            else "Source contains no CSV rows."
        ),
        page_index=0,
    )


def stopped(
    reader: str, blocks: list[TextBlock], findings: Sequence[TextReadFinding]
) -> TextReadResult:
    """Keep rows completed before the problem; return nothing after it."""
    return TextReadResult(
        reader=reader,
        outcome="partial" if blocks else "failed",
        blocks=tuple(blocks),
        findings=tuple(findings),
    )
