"""Translate backend geometry into the displayed, cropped PDF coordinate space."""

from math import isfinite
from typing import TYPE_CHECKING
from ...models import BoundingBox

if TYPE_CHECKING:
    from pdfplumber.page import Page


def displayed_crop(page: "Page") -> tuple[BoundingBox, float]:
    """Return the visible box in pdfplumber coordinates and the PDF unit scale.

    Compute the crop from PDF coordinates: merely swapping its axes misses the
    translation needed for asymmetric crops on rotated pages.
    """
    from pdfminer.pdftypes import resolve1

    media = [float(value) for value in page.page_obj.mediabox]
    crop = [float(value) for value in page.page_obj.cropbox]
    mx0, mx1 = sorted((media[0], media[2]))
    my0, my1 = sorted((media[1], media[3]))
    cx0, cx1 = sorted((crop[0], crop[2]))
    cy0, cy1 = sorted((crop[1], crop[3]))
    cx0, cy0 = max(mx0, cx0), max(my0, cy0)
    cx1, cy1 = min(mx1, cx1), min(my1, cy1)
    unit = float(resolve1(page.page_obj.attrs.get("UserUnit", 1)))
    if (
        not all(isfinite(value) for value in (*media, *crop, unit))
        or cx1 <= cx0
        or cy1 <= cy0
        or unit <= 0
        or page.rotation not in (0, 90, 180, 270)
    ):
        raise ValueError("unsupported page geometry")
    if page.rotation == 0:
        box = (cx0 - mx0, my1 - cy1, cx1 - mx0, my1 - cy0)
    elif page.rotation == 90:
        box = (cy0 - my0, cx0 - mx0, cy1 - my0, cx1 - mx0)
    elif page.rotation == 180:
        box = (mx1 - cx1, cy0 - my0, mx1 - cx0, cy1 - my0)
    else:
        box = (my1 - cy1, mx1 - cx1, my1 - cy0, mx1 - cx0)
    bx, by = page.mediabox[:2]
    return (box[0] + bx, box[1] + by, box[2] + bx, box[3] + by), unit


def local_box(box: BoundingBox, crop: BoundingBox, unit: float) -> BoundingBox | None:
    """Clip a backend rectangle to the visible crop and express it in points."""
    left, top = max(box[0], crop[0]), max(box[1], crop[1])
    right, bottom = min(box[2], crop[2]), min(box[3], crop[3])
    if right <= left or bottom <= top:
        return None
    return (
        (left - crop[0]) * unit,
        (top - crop[1]) * unit,
        (right - crop[0]) * unit,
        (bottom - crop[1]) * unit,
    )


def contains_center(container: BoundingBox, box: BoundingBox) -> bool:
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return container[0] <= x <= container[2] and container[1] <= y <= container[3]
