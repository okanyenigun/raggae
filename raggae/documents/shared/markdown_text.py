"""Markdown text as ``MarkdownTextReader`` reads it; shared by the Markdown text
reader and the Markdown table extractor.

CommonMark plus tables and strikethrough. Links contribute their labels and
images their alt text; nothing is rendered, fetched, or executed. Inline text
holding raw HTML is not read at all.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from markdown_it.token import Token


def parser() -> Any:
    """The markdown-it parser the Markdown readers use. Raises ImportError
    without markdown-it-py."""
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable(["table", "strikethrough"])


def inline_text(tokens: Sequence["Token"]) -> str | None:
    """Collect visible inline text; None reports unsupported raw HTML."""
    parts: list[str] = []
    for token in tokens:
        if token.type == "html_inline":
            return None
        if token.type in ("text", "code_inline"):
            parts.append(token.content)
        elif token.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif token.children:
            child_text = inline_text(token.children)
            if child_text is None:
                return None
            parts.append(child_text)
    return "".join(parts)
