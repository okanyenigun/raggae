"""Pipe tables in a Markdown document, with markdown-it-py."""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import failure, finding, finish, prepare_source, read_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...shared.markdown_text import inline_text, parser
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding

# One blockquote marker at the start of a line, as CommonMark reads it.
_QUOTE = re.compile(r"^ {0,3}> ?")
# A table written as raw HTML inside the Markdown.
_HTML_TABLE = re.compile(r"<\s*table\b", re.IGNORECASE)


class MarkdownTableExtractor:
    """Return the pipe tables in a Markdown document, with markdown-it-py.

    Every GFM pipe table, in document order, tables inside lists and quotes
    included; table syntax inside code is not a table. Each is an
    ``ExtractedTable`` with ``method="declared"`` and ``page_index=0``. The
    header row holds header cells, as GFM always declares one; the format has
    no spans, captions, or row headers. A row with fewer cells than the header
    keeps only the cells written (no padding); a row with more loses the
    extra ones, as GFM does, and says so (``fields_dropped``).

    A cell's text is what ``MarkdownTextReader`` reads from it: link labels,
    image alt text, code as written, emphasis markers removed. A cell holding
    raw HTML (``<br>`` included) is not read, as the text reader leaves out
    inline text with raw HTML; neither is a table written as raw HTML, which
    ``HtmlTableExtractor`` reads. Both are reported (``html_omitted``). A
    table with no text is not returned (``empty_table``).

    The source is decoded with ``encoding`` (UTF-8 by default), never guessed.
    Markdown has no layout regions, so any layout is refused before the source
    is opened.
    """

    _INFORMATIONAL = frozenset({TableFindingCode.EMPTY, TableFindingCode.EMPTY_TABLE})

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "table_extractor_markdown"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TableExtractResult:
        prepared = prepare_source(
            source,
            extractor=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message="Markdown has no layout regions; this extractor reads "
            "whole documents.",
        )
        if isinstance(prepared, TableExtractResult):
            return prepared
        data = read_source(prepared, self.name, allow_empty=True)
        if isinstance(data, TableExtractResult):
            return data
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )
        try:
            markdown = parser()
            from markdown_it.rules_block.table import escapedSplit
        except ImportError as error:
            raise ImportError(
                "Markdown table extraction requires: pip install 'raggae[text-markdown]'"
            ) from error
        try:
            tokens = markdown.parse(text)
        except (ValueError, RecursionError):
            return failure(
                self.name,
                TableFindingCode.PARSE_FAILED,
                "Markdown could not be parsed.",
            )
        # markdown-it numbers lines after turning every line ending into "\n".
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        notes: set[TableFindingCode] = set()
        found = False
        quotes = 0
        position = 0
        while position < len(tokens):
            token = tokens[position]
            if token.type == "blockquote_open":
                quotes += 1
            elif token.type == "blockquote_close":
                quotes -= 1
            elif token.type == "html_block" and _HTML_TABLE.search(token.content):
                notes.add(TableFindingCode.HTML_OMITTED)
            elif token.type == "table_open":
                found = True
                end = MarkdownTableExtractor._close(tokens, position)
                table = MarkdownTableExtractor._grid(
                    tokens[position:end], lines, quotes, escapedSplit, notes
                )
                if table is None:
                    findings.append(
                        finding(
                            TableFindingCode.EMPTY_TABLE,
                            "A table with no text in any cell was not returned.",
                            0,
                        )
                    )
                else:
                    tables.append(table)
                position = end
            position += 1
        messages = {
            TableFindingCode.HTML_OMITTED: (
                "Table cells holding raw HTML, and tables written as raw HTML, were "
                "not read; HtmlTableExtractor reads HTML tables."
            ),
            TableFindingCode.FIELDS_DROPPED: (
                "A table row has more cells than its header; the extra cells were "
                "dropped, as GFM drops them."
            ),
        }
        findings.extend(finding(code, messages[code], 0) for code in sorted(notes))
        if not found and not notes:
            findings.append(
                finding(TableFindingCode.EMPTY, "No tables in the document.", 0)
            )
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    @staticmethod
    def _close(tokens: list[Any], start: int) -> int:
        """The position of the ``table_close`` matching the ``table_open`` at ``start``."""
        depth = 0
        for position in range(start, len(tokens)):
            if tokens[position].type == "table_open":
                depth += 1
            elif tokens[position].type == "table_close":
                depth -= 1
                if depth == 0:
                    return position
        return len(tokens) - 1

    @staticmethod
    def _grid(
        tokens: list[Any],
        lines: list[str],
        quotes: int,
        split: Any,
        notes: set[TableFindingCode],
    ) -> ExtractedTable | None:
        """The table's cells; None when no cell has text."""
        rows: list[tuple[list[str], int | None]] = []
        current: list[str] | None = None
        line: int | None = None
        for token in tokens:
            if token.type == "tr_open":
                current, line = [], token.map[0] if token.map else None
            elif token.type == "tr_close" and current is not None:
                rows.append((current, line))
                current = None
            elif token.type == "inline" and current is not None:
                text = inline_text(token.children or [])
                if text is None:
                    notes.add(TableFindingCode.HTML_OMITTED)
                    text = ""
                current.append(text)
        if not rows:
            return None
        columns = len(rows[0][0])
        cells: list[TableCell] = []
        for number, (texts, line) in enumerate(rows):
            written = columns
            if number > 0 and line is not None and line < len(lines):
                written = MarkdownTableExtractor._count(lines[line], quotes, split)
                if written > columns:
                    notes.add(TableFindingCode.FIELDS_DROPPED)
            for column, text in enumerate(texts[: min(written, columns)]):
                cells.append(
                    TableCell(row=number, column=column, text=text, header=number == 0)
                )
        if not any(cell.text.strip() for cell in cells):
            return None
        return ExtractedTable(
            cells=tuple(cells),
            row_count=len(rows),
            column_count=columns,
            page_index=0,
            method="declared",
        )

    @staticmethod
    def _count(line: str, quotes: int, split: Any) -> int:
        """Cells written on a table row's source line, split as markdown-it splits it."""
        for _ in range(quotes):
            line = _QUOTE.sub("", line, count=1)
        cells = split(line.strip())
        if cells and cells[0] == "":
            cells.pop(0)
        if cells and cells[-1] == "":
            cells.pop()
        return len(cells)
