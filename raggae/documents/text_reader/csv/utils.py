"""CSV record framing and findings shared by the CSV reader family."""

import csv
import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult


@dataclass(frozen=True, slots=True)
class CsvRow:
    """One logical record and the one-based physical line on which it starts."""

    line: int
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CsvParseError:
    """Parsing stopped at this record; no later record can be trusted."""

    line: int
    field_too_large: bool


def require_delimiter(delimiter: str) -> str:
    if not isinstance(delimiter, str):
        raise TypeError("delimiter must be a string")
    if len(delimiter) != 1 or delimiter in '"\r\n':
        raise ValueError(
            "delimiter must be one character other than a quote or line break"
        )
    return delimiter


def require_header(header: bool) -> bool:
    if not isinstance(header, bool):
        raise TypeError("header must be a bool")
    return header


def parsed_rows(text: str, *, delimiter: str) -> Iterator[CsvRow | CsvParseError]:
    """Yield nonblank logical records in source order, keeping every field a string.

    Quoted fields may span lines and keep their original line endings. Only
    CR, LF, and CRLF end records; Unicode line separators are field content.
    Blank lines are skipped. Parsing ends at the first malformed record.
    """
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        skipinitialspace=False,
        strict=True,
    )
    consumed = 0
    while True:
        start = consumed + 1
        try:
            fields = next(reader)
        except StopIteration:
            return
        except csv.Error as error:
            # The stdlib limit is process-global; report it instead of changing it.
            yield CsvParseError(
                line=start,
                field_too_large=str(error).startswith("field larger than field limit"),
            )
            return
        consumed = reader.line_num
        if fields:
            yield CsvRow(line=start, fields=tuple(fields))


def column_label(index: int) -> str:
    """One-based fallback label for headerless or unnamed columns."""
    return f"Column {index + 1}"


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


def unique_names(names: Sequence[str]) -> tuple[tuple[str, ...], list[int]]:
    """Keep first occurrences; give repeats the next suffix no column uses."""
    originals = set(names)
    used: set[str] = set()
    last_suffix: dict[str, int] = {}
    keys: list[str] = []
    renamed: list[int] = []
    for index, name in enumerate(names):
        if name not in used:
            key = name
        else:
            suffix = last_suffix.get(name, 1)
            while True:
                suffix += 1
                key = f"{name}_{suffix}"
                if key not in originals and key not in used:
                    break
            last_suffix[name] = suffix
            renamed.append(index)
        used.add(key)
        keys.append(key)
    return tuple(keys), renamed
