"""PDF-space geometry shared by readers that select characters by centre."""

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


def inside(area: Box, box: Box) -> bool:
    """Centre of ``box`` inside ``area`` (both in PDF space)."""
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


def user_unit(value: Any) -> float:
    unit = float(value)
    if not isfinite(unit) or unit <= 0:
        raise ValueError("invalid UserUnit")
    return unit
