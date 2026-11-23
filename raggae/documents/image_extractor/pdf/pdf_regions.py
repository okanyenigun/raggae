"""Placing layout regions on PDF pages, shared by the PDF image extractors.

Opening a PDF and walking its drawn pictures use PDFium; placing regions and
matching them to pictures work from positions alone.
"""

import warnings
from collections.abc import Sequence
from io import BytesIO
from math import isclose
from stat import S_ISREG
from typing import Any
from ..models import ImageExtractFinding, ImageExtractResult, ImageFindingCode
from ..utils import failure
from ...layout_analysis.models import LayoutRegion, LayoutSurface, PdfPageLocation
from ...normalization.artifacts import Artifact
from ...pdf_geometry import Box, displayed_size, inside, user_unit, visible_crop


def open_document(
    source: Artifact, extractor: str
) -> tuple[Any, list[float]] | ImageExtractResult:
    """The PDFium document and each page's UserUnit, or a failed result."""
    try:
        if source.path is not None and not S_ISREG(source.path.stat().st_mode):
            return failure(
                extractor,
                ImageFindingCode.SOURCE_UNREADABLE,
                "Source must be a regular file.",
            )
        data = source.read_bytes()
    except OSError:
        return failure(
            extractor, ImageFindingCode.SOURCE_UNREADABLE, "Source could not be read."
        )
    if not data:
        return failure(extractor, ImageFindingCode.DECODE_FAILED, "Source is empty.")
    if b"%PDF-" not in data[:1024]:
        return ImageExtractResult(
            extractor=extractor,
            outcome="not_applicable",
            findings=(
                ImageExtractFinding(
                    code=ImageFindingCode.NOT_APPLICABLE,
                    message="Source has no PDF header.",
                ),
            ),
        )
    try:
        import pikepdf
        import pypdfium2 as pdfium
    except ImportError as error:
        raise ImportError(
            "PDFium image extraction requires: pip install 'raggae[image-pdf]'"
        ) from error
    password = source.password
    try:
        document = pdfium.PdfDocument(data, password=password)
    except pdfium.PdfiumError:
        # PDFium's error code can be left over from an earlier failure; it sets
        # none for a valid PDF without pages. Ask pikepdf why the file did not open.
        code, message = _refusal(pikepdf, data, password)
        return failure(extractor, code, message)
    try:
        # PDFium ignores /UserUnit; read it from the same file with pikepdf,
        # silencing its notice about a password an open PDF did not need.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with pikepdf.Pdf.open(BytesIO(data), password=password or "") as metadata:
                units = [
                    user_unit(page.obj.get("/UserUnit", 1)) for page in metadata.pages
                ]
        if len(units) != len(document):
            raise ValueError("page count mismatch")
    except (pikepdf.PdfError, ValueError, TypeError, KeyError):
        document.close()
        return failure(
            extractor, ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
        )
    if not len(document):
        document.close()
        return failure(
            extractor, ImageFindingCode.NO_PAGES, "PDF contains no usable pages."
        )
    return document, units


def _refusal(
    pikepdf: Any, data: bytes, password: str | None
) -> tuple[ImageFindingCode, str]:
    """Why PDFium refused the file, as pikepdf sees it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with pikepdf.Pdf.open(BytesIO(data), password=password or "") as check:
                pages = len(check.pages)
    except pikepdf.PasswordError:
        code = (
            ImageFindingCode.INVALID_PASSWORD
            if password
            else ImageFindingCode.PASSWORD_REQUIRED
        )
        return code, "PDF could not be opened with the supplied password."
    except (pikepdf.PdfError, ValueError, OSError):
        return ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
    if pages == 0:
        return ImageFindingCode.NO_PAGES, "PDF contains no usable pages."
    return ImageFindingCode.DECODE_FAILED, "PDF could not be opened."


def page_geometry(page: Any, unit: float) -> tuple[Box, int, tuple[float, float]]:
    """The visible crop (PDF space), the rotation, and the displayed size in points."""
    rotation = page.get_rotation()
    crop = visible_crop(page.get_mediabox(), page.get_cropbox(), rotation)
    return crop, rotation, displayed_size(crop, rotation, unit)


def drawn_pictures(page: Any, max_depth: int = 15) -> list[tuple[Any, Box]]:
    """Each picture drawn on the page, in drawing order, with its PDF-space bounds.

    Inline images and pictures inside forms are included. PDFium gives a nested
    picture's bounds in its form's space, so they are carried out through each
    enclosing form's matrix. Forms nested deeper than ``max_depth`` are not
    followed.
    """
    import pypdfium2.raw as raw

    found: list[tuple[Any, Box]] = []

    def walk(objects: Any, matrices: list[Any], depth: int) -> None:
        for item in objects:
            if item.type == raw.FPDF_PAGEOBJ_IMAGE:
                left, bottom, right, top = item.get_bounds()
                corners = [(left, bottom), (right, top), (left, top), (right, bottom)]
                for matrix in reversed(matrices):
                    corners = [matrix.on_point(x, y) for x, y in corners]
                xs, ys = [x for x, _ in corners], [y for _, y in corners]
                found.append((item, (min(xs), min(ys), max(xs), max(ys))))
            elif item.type == raw.FPDF_PAGEOBJ_FORM and depth < max_depth:
                walk(
                    page.get_objects(max_depth=0, form=item, level=item.level + 1),
                    [*matrices, item.get_matrix()],
                    depth + 1,
                )

    walk(page.get_objects(max_depth=0), [], 0)
    return found


def place_regions(
    selected: tuple[LayoutRegion, ...],
    surfaces: dict[str, LayoutSurface],
    page_count: int,
) -> tuple[dict[int, list[LayoutRegion]], list[ImageExtractFinding]]:
    """Group PDF page regions by zero-based page; explain the ones that cannot be used."""
    pending: dict[int, list[LayoutRegion]] = {}
    findings: list[ImageExtractFinding] = []
    for region in selected:
        location = surfaces[region.surface_id].location
        if not isinstance(location, PdfPageLocation) or region.bbox is None:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.SELECTION_UNSUPPORTED,
                    message="Selection is not a region on a PDF page.",
                    region_id=region.id,
                )
            )
        elif location.page_number > page_count:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.PAGE_MISSING,
                    message="Requested page does not exist in this PDF.",
                    region_id=region.id,
                    page_index=location.page_number - 1,
                )
            )
        else:
            pending.setdefault(location.page_number - 1, []).append(region)
    return pending, findings


def claim(
    bounds: Sequence[Box], areas: Sequence[Box]
) -> tuple[list[list[int]], set[int]]:
    """Which pictures (by position in ``bounds``) each area holds, by centre.

    Returns the pictures per area, and the areas that share a picture; those
    are refused, so a picture never comes back for two regions.
    """
    held: list[list[int]] = [[] for _ in areas]
    conflicts: set[int] = set()
    for number, box in enumerate(bounds):
        owners = [area for area, region in enumerate(areas) if inside(region, box)]
        if len(owners) > 1:
            conflicts.update(owners)
        for area in owners:
            held[area].append(number)
    return held, conflicts


def fits_page(surface: LayoutSurface, size: tuple[float, float]) -> bool:
    """Whether the layout surface has the displayed size of this page."""
    width, height = size
    return (
        surface.width is not None
        and surface.height is not None
        and isclose(surface.width, width, abs_tol=0.01, rel_tol=1e-6)
        and isclose(surface.height, height, abs_tol=0.01, rel_tol=1e-6)
    )


def mismatch(region: LayoutRegion, index: int) -> ImageExtractFinding:
    return ImageExtractFinding(
        code=ImageFindingCode.LAYOUT_MISMATCH,
        message="Layout dimensions do not match the source page.",
        region_id=region.id,
        page_index=index,
    )
