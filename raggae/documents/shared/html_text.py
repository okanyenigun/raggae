"""HTML element text as ``HtmlTextReader`` reads it; shared by the HTML text reader
and the HTML table extractor.

Whitespace collapses as a browser collapses it outside ``<pre>``; ``<br>`` and
block elements break lines; image alt text is read. Scripts, styles,
templates, ``noscript``, and ``<head>`` are never read. Elements with the
``hidden`` attribute and embedded, interactive, or vector elements are not
read either, and ``notes`` collects the text-reader finding codes that say so.
Nothing is fetched or executed, and CSS is not applied.

Functions take BeautifulSoup ``html.parser`` nodes. ``excluded`` holds the
``id()`` of elements whose content belongs to another selected region.
"""

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..text_reader.models import FindingCode

# Element groups follow the HTML layout analyzer so both stages agree.
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
TEXT_BLOCKS = frozenset({"p", "pre", "li", "dt", "dd", "blockquote", "figcaption"})
UNSUPPORTED = frozenset(
    {
        "svg",
        "math",
        "iframe",
        "object",
        "embed",
        "canvas",
        "video",
        "audio",
        "input",
        "button",
        "select",
        "textarea",
    }
)
# Never text: executable, styling, inert, or document-metadata elements.
SKIPPED = frozenset({"script", "style", "template", "noscript", "head", "title"})
BLOCK_LEVEL = (
    HEADINGS
    | TEXT_BLOCKS
    | frozenset(
        {
            "address",
            "article",
            "aside",
            "body",
            "caption",
            "center",
            "details",
            "dialog",
            "dir",
            "div",
            "dl",
            "fieldset",
            "figure",
            "footer",
            "form",
            "header",
            "hgroup",
            "hr",
            "html",
            "legend",
            "main",
            "menu",
            "nav",
            "ol",
            "search",
            "section",
            "summary",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )
)


class TextLines:
    """Assemble lines the way a browser collapses whitespace outside <pre>."""

    _COLLAPSIBLE = re.compile(r"[ \t\n\r\f]+")

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._current = ""

    @staticmethod
    def collapse(text: str) -> str:
        """Runs of HTML whitespace become one space."""
        return TextLines._COLLAPSIBLE.sub(" ", text)

    def inline(self, text: str) -> None:
        text = self.collapse(text)
        if not self._current or self._current.endswith(" "):
            text = text.lstrip(" ")
        self._current += text

    def hard_break(self) -> None:
        self._lines.append(self._current.rstrip(" "))
        self._current = ""

    def soft_break(self) -> None:
        if self._current.strip(" "):
            self.hard_break()
        self._current = ""

    def verbatim(self, text: str | None) -> None:
        self.soft_break()
        if text is not None:
            self._lines.append(text)

    def result(self) -> str | None:
        self.soft_break()
        lines = self._lines
        while lines and not lines[0].strip():
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        return "\n".join(lines) if lines else None


def element_text(
    element: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str | None:
    """Text of one element; descendants in ``excluded`` belong to other blocks."""
    name = element.name
    if name == "img":
        text = TextLines.collapse(alt(element)).strip(" ")
        return text if text.strip() else None
    if name == "table":
        return table_text(element, excluded, notes)
    if name == "pre":
        return pre_text(element, excluded, notes)
    lines = TextLines()
    render(element, lines, excluded, notes)
    return lines.result()


def render(
    node: Any,
    text: TextLines,
    excluded: frozenset[int],
    notes: "set[FindingCode]",
) -> None:
    from ..text_reader.models import FindingCode

    for child in node.children:
        if not is_tag(child):
            if is_text(child):
                text.inline(str(child))
            continue
        name = child.name
        if id(child) in excluded:
            if name in BLOCK_LEVEL:
                text.soft_break()
            continue
        if name in SKIPPED:
            continue
        if child.has_attr("hidden"):
            notes.add(FindingCode.HIDDEN_SKIPPED)
        elif name in UNSUPPORTED:
            notes.add(FindingCode.UNSUPPORTED_CONTENT)
        elif name == "br":
            text.hard_break()
        elif name == "img":
            text.inline(alt(child))
        elif name in ("pre", "table"):
            text.verbatim(element_text(child, excluded, notes))
        elif name in BLOCK_LEVEL:
            text.soft_break()
            render(child, text, excluded, notes)
            text.soft_break()
        else:
            render(child, text, excluded, notes)


def pre_text(
    element: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str | None:
    from ..text_reader.models import FindingCode

    pieces: list[str] = []

    def collect(node: Any) -> None:
        for child in node.children:
            if not is_tag(child):
                if is_text(child):
                    pieces.append(str(child))
                continue
            if id(child) in excluded or child.name in SKIPPED:
                continue
            if child.has_attr("hidden"):
                notes.add(FindingCode.HIDDEN_SKIPPED)
            elif child.name in UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)
            elif child.name == "br":
                pieces.append("\n")
            elif child.name == "img":
                pieces.append(alt(child))
            else:
                collect(child)

    collect(element)
    content = "".join(pieces)
    # As in browsers, one newline directly after <pre> is not content.
    content = (
        content.removeprefix("\r\n")
        if content.startswith("\r\n")
        else content.removeprefix("\n")
    )
    return content if content.strip() else None


def table_text(
    table: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str | None:
    """A caption line, then one row per line with cells joined by `` | ``."""
    lines: list[str] = []
    for child in table.children:
        if is_tag(child) and child.name == "caption" and id(child) not in excluded:
            if (caption := element_text_inline(child, excluded, notes)) is not None:
                lines.append(caption)
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table or not visible_in(row, table, notes):
            continue
        if id(row) in excluded:
            continue
        cells = [
            element_text_inline(cell, excluded, notes) or ""
            for cell in row.children
            if is_tag(cell)
            and cell.name in ("td", "th")
            and id(cell) not in excluded
            and visible_in(cell, table, notes)
        ]
        if any(cell.strip() for cell in cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines) if lines else None


def element_text_inline(
    element: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str | None:
    """Cell and caption text on one line, so each table row stays one line."""
    text = TextLines()
    render(element, text, excluded, notes)
    content = text.result()
    if content is None:
        return None
    return " ".join(line.strip() for line in content.split("\n") if line.strip())


def visible_in(element: Any, table: Any, notes: "set[FindingCode]") -> bool:
    """Whether nothing between the element and its table is hidden or never read."""
    from ..text_reader.models import FindingCode

    node: Any = element
    while node is not None and node is not table:
        if node.has_attr("hidden"):
            notes.add(FindingCode.HIDDEN_SKIPPED)
            return False
        if node.name in SKIPPED:
            return False
        node = node.parent
    return True


def resolve(document: Any, indexes: Sequence[int]) -> Any:
    """The element at element-only child ``indexes`` from the parser root, or None."""
    node = document
    for index in indexes:
        children = [child for child in node.children if is_tag(child)]
        if index >= len(children):
            return None
        node = children[index]
    return node


def lineage(element: Any) -> list[Any]:
    """The element and its ancestor elements, excluding the document root."""
    chain: list[Any] = []
    node = element
    while node is not None and node.parent is not None:
        chain.append(node)
        node = node.parent
    return chain


def alt(element: Any) -> str:
    value = element.get("alt")
    return value if isinstance(value, str) else ""


def is_tag(node: Any) -> bool:
    from bs4.element import Tag

    return isinstance(node, Tag)


def is_text(node: Any) -> bool:
    from bs4.element import (
        CData,
        Comment,
        Declaration,
        Doctype,
        NavigableString,
        ProcessingInstruction,
    )

    return isinstance(node, NavigableString) and not isinstance(
        node, (CData, Comment, Declaration, Doctype, ProcessingInstruction)
    )
