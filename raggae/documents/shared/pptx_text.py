"""Slide shape text as ``PptxTextReader`` reads it; shared by the PowerPoint text
reader and the PowerPoint table extractor.

Functions take python-pptx objects. Slides come in presentation order, from the
slide list, and shapes in the order they are stored on a slide; group members
are reached through their group.
"""

from collections.abc import Sequence
from typing import Any

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def slides(presentation: Any) -> list[Any]:
    """The slides in presentation order.

    Never use ``presentation.slides``: it renames parts to match slide order.
    """
    slide_ids = presentation.element.sldIdLst
    return [
        presentation.part.related_slide(slide_id.rId)
        for slide_id in (slide_ids if slide_ids is not None else ())
    ]


def kind(shape: Any) -> str:
    """group, picture, text, connector, table, or unsupported (charts, SmartArt,
    embedded objects)."""
    tag = shape.element.tag
    if tag == f"{P}grpSp":
        return "group"
    if tag == f"{P}pic":
        return "picture"
    if tag == f"{P}sp":
        return "text"
    if tag == f"{P}cxnSp":
        return "connector"
    if tag == f"{P}graphicFrame" and shape.has_table:
        return "table"
    return "unsupported"


def body_text(body: Any) -> str | None:
    """One line per paragraph; edge blank paragraphs dropped."""
    lines = [paragraph_text(paragraph) for paragraph in body.iter(f"{A}p")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) if lines else None


def paragraph_text(paragraph: Any) -> str:
    """Runs and fields as written; bullet symbols and numbering are not added."""
    pieces: list[str] = []
    for child in paragraph:
        if child.tag in (f"{A}r", f"{A}fld"):
            text = child.find(f"{A}t")
            pieces.append("" if text is None else text.text or "")
        elif child.tag == f"{A}br":
            pieces.append("\n")
    return "".join(pieces)


def hidden(slide: Any) -> bool:
    """Whether the slide is hidden in slide shows."""
    return slide.element.get("show") in ("0", "false")


def resolve(slide: Any, path: Sequence[int]) -> Any:
    """The shape at zero-based ``path`` through slide shapes and group shapes, or None."""
    shapes = slide.shapes
    shape = None
    for depth, index in enumerate(path):
        members = list(shapes)
        if index >= len(members):
            return None
        shape = members[index]
        if depth < len(path) - 1:
            if kind(shape) != "group":
                return None
            shapes = shape.shapes
    return shape
