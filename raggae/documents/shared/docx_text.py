"""Word main-body text as ``DocxTextReader`` reads it; shared by the Word text
reader and the Word table extractor.

Text is read as if tracked changes were accepted: insertions kept, deletions
left out. Alternate content is read from its ``mc:Choice``. Image alt text is
read; text boxes, equations, embedded objects, and imported content are not,
and ``notes`` collects the text-reader finding codes that say so.

Functions take python-docx's lxml elements. ``excluded`` holds the ``id()`` of
elements whose content belongs to another selected region.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..text_reader.models import FindingCode

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
V = "{urn:schemas-microsoft-com:vml}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

DRAWINGS = frozenset({f"{W}drawing", f"{W}pict"})
# Containers whose children are ordinary content in the accepted view.
TRANSPARENT = frozenset(
    {
        f"{W}hyperlink",
        f"{W}smartTag",
        f"{W}customXml",
        f"{W}fldSimple",
        f"{W}ins",
        f"{W}moveTo",
        f"{W}dir",
        f"{W}bdo",
        f"{W}sdtContent",
        f"{W}sdt",
    }
)
DELETED = frozenset({f"{W}del", f"{W}moveFrom"})
REVISIONS = DELETED | {f"{W}ins", f"{W}moveTo"}
UNSUPPORTED = frozenset({f"{W}object", f"{W}altChunk", f"{M}oMath", f"{M}oMathPara"})
RUN_TEXT = {
    f"{W}t": None,
    f"{W}tab": "\t",
    f"{W}ptab": "\t",
    f"{W}br": "\n",
    f"{W}cr": "\n",
    f"{W}noBreakHyphen": "-",
}
# Content controls and custom XML: their children are body-level content.
WRAPPERS = frozenset({f"{W}sdt", f"{W}sdtContent", f"{W}customXml"})


def paragraph_text(
    paragraph: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str:
    from ..text_reader.models import FindingCode

    pieces: list[str] = []

    def visit(node: Any) -> None:
        for child in node:
            tag = child.tag
            if id(child) in excluded or not isinstance(tag, str):
                continue
            if tag in REVISIONS:
                notes.add(FindingCode.REVISIONS_PRESENT)
            if tag in DELETED:
                continue
            if tag == f"{W}r":
                visit_run(child)
            elif tag in TRANSPARENT:
                visit(child)
            elif tag == f"{MC}AlternateContent":
                choice = child.find(f"{MC}Choice")
                if choice is not None:
                    visit(choice)
            elif tag in UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)

    def visit_run(run: Any) -> None:
        for child in run:
            tag = child.tag
            if id(child) in excluded or not isinstance(tag, str):
                continue
            if tag == f"{W}t":
                pieces.append(child.text or "")
            elif tag in RUN_TEXT:
                pieces.append(RUN_TEXT[tag] or "")
            elif tag in DRAWINGS:
                if is_text_box(child):
                    notes.add(FindingCode.TEXT_BOXES_UNREAD)
                elif text := alt_text(child):
                    pieces.append(text)
            elif tag == f"{MC}AlternateContent":
                choice = child.find(f"{MC}Choice")
                if choice is not None:
                    visit_run(choice)
            elif tag in UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)

    visit(paragraph)
    return "".join(pieces)


def table_text(
    table: Any, excluded: frozenset[int], notes: "set[FindingCode]"
) -> str | None:
    """One row per line, cells joined by `` | ``; deleted rows left out."""
    from ..text_reader.models import FindingCode

    lines: list[str] = []
    for row in children(table, f"{W}tr"):
        properties = row.find(f"{W}trPr")
        if properties is not None and properties.find(f"{W}del") is not None:
            notes.add(FindingCode.REVISIONS_PRESENT)
            continue
        cells = [cell_text(cell, excluded, notes) for cell in children(row, f"{W}tc")]
        if any(cell.strip() for cell in cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines) if lines else None


def cell_text(cell: Any, excluded: frozenset[int], notes: "set[FindingCode]") -> str:
    """One line per cell, so each table row stays on one line."""
    parts: list[str] = []
    for child in children(cell, f"{W}p", f"{W}tbl"):
        text = (
            paragraph_text(child, excluded, notes)
            if child.tag == f"{W}p"
            else table_text(child, excluded, notes)
        )
        if text is not None and text.strip():
            parts.append(" ".join(text.split()))
    return " ".join(parts)


def children(node: Any, *tags: str) -> list[Any]:
    """Direct children with these tags, looking through content-control wrappers."""
    found: list[Any] = []
    for child in node:
        if child.tag in tags:
            found.append(child)
        elif child.tag in WRAPPERS:
            found.extend(children(child, *tags))
    return found


def alt_text(drawing: Any) -> str | None:
    if is_text_box(drawing):
        return None
    for properties in drawing.iter(f"{WP}docPr"):
        if description := properties.get("descr"):
            return " ".join(description.split()) or None
    for shape in drawing.iter(f"{V}shape", f"{V}rect"):
        if description := shape.get("alt"):
            return " ".join(description.split()) or None
    return None


def is_text_box(drawing: Any) -> bool:
    return next(drawing.iter(f"{W}txbxContent"), None) is not None


def unreadable(element: Any) -> str | None:
    """Why the text reader cannot read this element as a region, or None."""
    tag = element.tag
    if any(ancestor.tag == f"{MC}Fallback" for ancestor in element.iterancestors()):
        return "This element is an alternate representation of content read elsewhere."
    if tag in DRAWINGS and is_text_box(element):
        return "Text boxes are not read."
    if tag in (f"{W}p", f"{W}tbl", f"{W}sdt", f"{W}customXml") or tag in DRAWINGS:
        return None
    return "This element's content is outside what DocxTextReader reads."


def resolve(body: Any, indexes: Sequence[int]) -> Any:
    """The element at zero-based child ``indexes`` from ``w:body``, or None."""
    node = body
    for index in indexes:
        elements = list(node)
        if index >= len(elements):
            return None
        node = elements[index]
    return node
