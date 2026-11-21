"""Helpers shared by the Unstructured-backed readers' format modules."""

import json
from collections.abc import Callable
from io import BytesIO
from typing import Any
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)


def element_text(element: Any) -> str:
    """Element text; tables become rows on lines with cells joined by `` | ``."""
    html = getattr(element.metadata, "text_as_html", None)
    if type(element).__name__ == "Table" and html:
        from bs4 import BeautifulSoup

        rows = []
        for row in BeautifulSoup(html, "html.parser").find_all("tr"):
            cells = [
                " ".join(cell.get_text(" ").split())
                for cell in row.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            return "\n".join(rows)
    return (element.text or "").strip()


def binary_file(data: str | bytes) -> BytesIO:
    assert isinstance(data, bytes)
    return BytesIO(data)


def text_of(data: str | bytes) -> str:
    assert isinstance(data, str)
    return data


def lines_result(
    reader: str,
    text: str,
    partition: Callable[[str], list[Any]],
) -> TextReadResult:
    """One partition per JSON Lines record; blocks keep their source line."""
    blocks: list[TextBlock] = []
    findings: list[TextReadFinding] = []
    completed = 0
    if text:
        for number, line in enumerate(text.removesuffix("\n").split("\n"), start=1):
            line = line.removesuffix("\r")
            try:
                json.loads(line)
                elements = partition(line)
            except Exception:  # noqa: BLE001 - invalid JSON or partition failure
                findings.append(
                    TextReadFinding(
                        code=FindingCode.PARSE_FAILED,
                        message=f"Line {number} could not be read as one JSON value.",
                        page_index=0,
                    )
                )
                continue
            completed += 1
            for element in elements:
                if value := element_text(element):
                    blocks.append(
                        TextBlock(
                            text=json.dumps({"line": number, "value": value}),
                            page_index=0,
                            content_format=TextContentFormat.JSON,
                        )
                    )
    if not blocks and not findings:
        findings.append(
            TextReadFinding(
                code=FindingCode.EMPTY,
                message="Source contains no JSON Lines records.",
                page_index=0,
            )
        )
    incomplete = any(f.code == FindingCode.PARSE_FAILED for f in findings)
    return TextReadResult(
        reader=reader,
        outcome=("partial" if completed else "failed") if incomplete else "success",
        blocks=tuple(blocks),
        findings=tuple(findings),
    )
