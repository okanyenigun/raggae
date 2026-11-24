"""PDF geometry and character assembly shared by PDF text readers, image extractors,
and table extractors."""

from math import isfinite
from typing import Any

type Box = tuple[float, float, float, float]  # PDF space: left, bottom, right, top


def visible_crop(media: Any, crop: Any, rotation: int) -> Box:
    """The crop box clipped to the media box, in PDF space."""
    mx0, mx1 = sorted((float(media[0]), float(media[2])))
    my0, my1 = sorted((float(media[1]), float(media[3])))
    cx0, cx1 = sorted((float(crop[0]), float(crop[2])))
    cy0, cy1 = sorted((float(crop[1]), float(crop[3])))
    box = (max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1))
    if (
        not all(isfinite(value) for value in box)
        or box[2] <= box[0]
        or box[3] <= box[1]
        or rotation not in (0, 90, 180, 270)
    ):
        raise ValueError("unsupported page geometry")
    return box


def displayed_size(crop: Box, rotation: int, unit: float) -> tuple[float, float]:
    """Width and height of the visible area as displayed, in points."""
    width, height = crop[2] - crop[0], crop[3] - crop[1]
    if rotation in (90, 270):
        width, height = height, width
    return width * unit, height * unit


def pdf_box(bbox: Any, crop: Box, rotation: int, unit: float) -> Box:
    """Displayed-crop, top-left, UserUnit-scaled points -> PDF space."""
    cx0, cy0, cx1, cy1 = crop
    corners = []
    for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[3])):
        x, y = x / unit, y / unit
        if rotation == 0:
            corners.append((cx0 + x, cy1 - y))
        elif rotation == 90:
            corners.append((cx0 + y, cy0 + x))
        elif rotation == 180:
            corners.append((cx1 - x, cy0 + y))
        else:
            corners.append((cx1 - y, cy1 - x))
    (ax, ay), (bx, by) = corners
    return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)


def displayed_box(box: Box, crop: Box, rotation: int, unit: float) -> Box:
    """PDF space -> displayed-crop, top-left, UserUnit-scaled points; undoes pdf_box."""
    cx0, cy0, cx1, cy1 = crop
    corners = []
    for x, y in ((box[0], box[1]), (box[2], box[3])):
        if rotation == 0:
            corners.append((x - cx0, cy1 - y))
        elif rotation == 90:
            corners.append((y - cy0, x - cx0))
        elif rotation == 180:
            corners.append((cx1 - x, y - cy0))
        else:
            corners.append((cy1 - y, cx1 - x))
    (ax, ay), (bx, by) = corners
    return (
        min(ax, bx) * unit,
        min(ay, by) * unit,
        max(ax, bx) * unit,
        max(ay, by) * unit,
    )


def inside(area: Box, box: Box) -> bool:
    """Centre of ``box`` inside ``area`` (both in PDF space)."""
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


def user_unit(value: Any) -> float:
    unit = float(value)
    if not isfinite(unit) or unit <= 0:
        raise ValueError("invalid UserUnit")
    return unit


def unrotated_characters(
    characters: list[dict[str, Any]],
    rotation: int,
) -> list[dict[str, Any]]:
    """Undo page rotation for word assembly, after selecting displayed boxes.

    ``characters`` are pdfplumber character objects, in pdfplumber's displayed
    coordinates; ``rotation`` is the page's quarter-turn rotation.

    Otherwise a 180-degree page can reverse a word's letters. These copies
    affect only text grouping; supplied layout geometry is unchanged.
    Relative coordinates suffice, so no origin translation is needed here.
    """
    if rotation == 0:
        return characters
    result: list[dict[str, Any]] = []
    for char in characters:
        left, top, right, bottom = (
            char["x0"],
            char["top"],
            char["x1"],
            char["bottom"],
        )
        if rotation == 90:
            box = (top, -right, bottom, -left)
        elif rotation == 180:
            box = (-right, -bottom, -left, -top)
        else:  # displayed_crop already validates quarter-turn rotations.
            box = (-bottom, left, -top, right)
        x0, y0, x1, y1 = box
        result.append(
            {
                **char,
                "x0": x0,
                "top": y0,
                "x1": x1,
                "bottom": y1,
                "width": x1 - x0,
                "height": y1 - y0,
                "doctop": y0,
                "upright": not char["upright"]
                if rotation in (90, 270)
                else char["upright"],
            }
        )
    return result


def assemble(
    characters: list[tuple[str, Box]], owners: list[list[int]], scope: int
) -> str:
    """Join a scope's characters, keeping one space or line break between them.

    Whitespace (PDFium's generated or stored) and characters of other scopes
    become a single separator; a line break wins over a space.
    """
    pieces: list[str] = []
    newline = space = False
    for (character, _), found in zip(characters, owners):
        if character in "\r\n":
            newline = True
        elif character.isspace() or scope not in found:
            space = True
        else:
            if pieces and (newline or space):
                pieces.append("\n" if newline else " ")
            pieces.append(character)
            newline = space = False
    return "".join(pieces)
