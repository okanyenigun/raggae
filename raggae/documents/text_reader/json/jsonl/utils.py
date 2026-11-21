"""Line framing and output handling shared by the two JSON Lines readers."""

from collections.abc import Iterator
from typing import Any
from ..utils import DuplicateJsonKeyError, loads_json
from ...models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)


def parsed_lines(
    text: str,
) -> Iterator[tuple[int, str, Any] | TextReadFinding]:
    """Parse LF-delimited records independently; never split Unicode separators."""
    if not text:
        return
    # A final LF terminates the last record; it does not create a blank record.
    for line_number, line in enumerate(text.removesuffix("\n").split("\n"), start=1):
        try:
            value = loads_json(line)
        except DuplicateJsonKeyError:
            yield TextReadFinding(
                code=FindingCode.DUPLICATE_KEY,
                message=f"Line {line_number} contains duplicate object keys; this record was not returned.",
                page_index=0,
            )
        except (ValueError, RecursionError):
            yield TextReadFinding(
                code=FindingCode.PARSE_FAILED,
                message=f"Line {line_number} could not be parsed as one JSON value.",
                page_index=0,
            )
        else:
            yield line_number, line, value


def record_block(line_number: int, value_text: str) -> TextBlock:
    """Keep source-line provenance in content, not new common-model fields."""
    return TextBlock(
        text='{"line":' + str(line_number) + ',"value":' + value_text + "}",
        page_index=0,
        content_format=TextContentFormat.JSON,
    )


def finish_read(
    *,
    reader: str,
    blocks: list[TextBlock],
    findings: list[TextReadFinding],
    completed: int,
    incomplete: bool,
) -> TextReadResult:
    if not blocks and not findings:
        findings.append(
            TextReadFinding(
                code=FindingCode.EMPTY,
                message="Source contains no JSON Lines records.",
                page_index=0,
            )
        )
    return TextReadResult(
        reader=reader,
        outcome=("partial" if completed else "failed") if incomplete else "success",
        blocks=tuple(blocks),
        findings=tuple(findings),
    )
