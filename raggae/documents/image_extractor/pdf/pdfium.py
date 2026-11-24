"""Pictures from a PDF with PDFium: as they appear on the page, or as stored files."""

from collections.abc import Sequence
from contextlib import closing
from io import BytesIO
from math import isfinite
from pathlib import Path
from typing import Any, Literal
from PIL import Image, UnidentifiedImageError
from .pdf_regions import (
    claim,
    drawn_pictures,
    fits_page,
    mismatch,
    open_document,
    page_geometry,
    place_regions,
)
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    finding,
    image_file,
    nothing_selected,
    png,
    select_image_regions,
    too_large,
    too_many_pixels,
)
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion, LayoutSurface
from ...normalization.artifacts import Artifact
from ...shared.pages import PdfPages
from ...shared.pdf_geometry import displayed_box, inside, pdf_box
from ...shared.sources import as_artifact

type PdfiumMode = Literal["rendered", "stored"]
# Left, top, right, bottom in displayed points, measured from the page's top-left.
type DisplayedBox = tuple[float, float, float, float]
type PageOutcome = tuple[list[ExtractedImage], list[ImageExtractFinding], int, int]


class PdfiumImageExtractor:
    """Return a PDF's pictures with PDFium, as they appear on the page or as stored.

    Both modes find the same pictures: each picture drawn on a page whose centre
    lies in the visible area, inline images and pictures inside forms included,
    in drawing order. A picture drawn twice comes back twice; one listed but
    never drawn does not come back.

    ``mode="rendered"`` (default) draws each page on white at ``dpi`` (default
    300), without annotations or form fields, and crops what is visible where
    each picture is drawn: an opaque RGB PNG with ``method="rendered"``. Text
    printed over a picture comes with it, and a clipped picture keeps its whole
    rectangle. With a layout it crops each requested region, whatever is in it,
    so a chart drawn with lines can be extracted; overlapping regions are
    allowed.

    ``mode="stored"`` returns the picture file stored in the PDF behind each
    drawn picture, at its own resolution: a stored JPEG byte for byte
    (``original``), other pictures as new files (``reencoded``: PNG, or TIFF for
    CMYK). PDFium cannot apply masks and does not say when a picture has one,
    so a masked picture comes back without its transparency and without a
    finding. With a layout it returns the pictures whose drawn centre lies in
    each requested region; a region without one gets an ``empty`` finding, and
    regions claiming the same picture are both refused with
    ``selection_overlap``. ``dpi`` is not used.

    With a layout and no ``region_ids`` only image regions are used; explicit
    ``region_ids`` are used whatever their kind. Pages or pictures above
    Pillow's decompression-bomb ceiling are skipped with a finding. Passwords
    come from ``Artifact``.
    """

    _INFORMATIONAL = frozenset(
        {ImageFindingCode.EMPTY, ImageFindingCode.NOTHING_SELECTED}
    )

    def __init__(self, *, mode: PdfiumMode = "rendered", dpi: float = 300) -> None:
        if not isinstance(mode, str):
            raise TypeError("mode must be a string")
        if mode not in ("rendered", "stored"):
            raise ValueError("mode must be 'rendered' or 'stored'")
        if isinstance(dpi, bool) or not isinstance(dpi, (int, float)):
            raise TypeError("dpi must be a number")
        if not isfinite(dpi) or dpi <= 0:
            raise ValueError("dpi must be a positive, finite number")
        self._mode: PdfiumMode = mode
        self._dpi = float(dpi)

    @property
    def name(self) -> str:
        return "image_extractor_pdfium"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult:
        artifact = as_artifact(source)
        selected = select_image_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name, explicit=region_ids is not None)
        opened = open_document(artifact, self.name)
        if isinstance(opened, ImageExtractResult):
            return opened
        document, units = opened
        # PdfPages renders the pages and closes the document when done.
        pages = PdfPages(document, units, self._dpi)
        try:
            surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
            return self._extract(document, pages, units, selected, surfaces)
        finally:
            pages.close()

    def _extract(
        self,
        document: Any,
        pages: PdfPages,
        units: list[float],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> ImageExtractResult:
        import pypdfium2 as pdfium

        findings: list[ImageExtractFinding] = []
        if selected is None:
            plan: dict[int, list[LayoutRegion] | None] = dict.fromkeys(
                range(len(document))
            )
        else:
            pending, findings = place_regions(selected, surfaces, len(document))
            plan = dict(sorted(pending.items()))

        images: list[ExtractedImage] = []
        completed = drawn = 0
        for index, regions in plan.items():
            try:
                if self._mode == "rendered":
                    outcome = self._rendered_page(
                        document, pages, index, units[index], regions, surfaces
                    )
                else:
                    outcome = self._stored_page(
                        document, index, units[index], regions, surfaces
                    )
            except (pdfium.PdfiumError, RuntimeError, ValueError, TypeError, OSError):
                findings.extend(
                    finding(
                        ImageFindingCode.PAGE_UNREADABLE,
                        "This PDF page could not be rendered."
                        if self._mode == "rendered"
                        else "The pictures on this PDF page could not be read.",
                        index,
                        identifier,
                    )
                    for identifier in (
                        [None] if regions is None else [r.id for r in regions]
                    )
                )
                continue
            page_images, page_findings, page_completed, page_drawn = outcome
            images.extend(page_images)
            findings.extend(page_findings)
            completed += page_completed
            drawn += page_drawn

        if selected is None and completed and not drawn:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No pictures are drawn in this PDF. Charts drawn with "
                    "lines are found only through a layout region, in rendered mode.",
                )
            )
        incomplete = any(
            f.code not in PdfiumImageExtractor._INFORMATIONAL for f in findings
        )
        return ImageExtractResult(
            extractor=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            images=tuple(images),
            findings=tuple(findings),
        )

    # Rendered mode -----------------------------------------------------------

    def _rendered_page(
        self,
        document: Any,
        pages: PdfPages,
        index: int,
        unit: float,
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> PageOutcome:
        """One page's crops, findings, completed work, and pictures drawn.

        Without a layout, a searched page counts as completed work even when
        nothing is drawn on it; with a layout, each cropped region does.
        """
        findings: list[ImageExtractFinding] = []
        boxes: list[tuple[str | None, DisplayedBox]] = []
        with closing(document[index]) as page:
            crop, rotation, size = page_geometry(page, unit)
            if regions is None:
                for _, bounds in drawn_pictures(page):
                    if inside(crop, bounds):
                        boxes.append(
                            (None, displayed_box(bounds, crop, rotation, unit))
                        )
            else:
                for region in regions:
                    if region.bbox is None or not fits_page(
                        surfaces[region.surface_id], size
                    ):
                        findings.append(mismatch(region, index))
                    else:
                        boxes.append((region.id, region.bbox))
        drawn = len(boxes) if regions is None else 0
        if not boxes:
            return [], findings, 1 if regions is None else 0, drawn
        if too_many_pixels(size[0] / 72 * self._dpi, size[1] / 72 * self._dpi):
            findings.extend(
                finding(
                    ImageFindingCode.IMAGE_TOO_LARGE,
                    "This page is above Pillow's decompression-bomb limit at this "
                    "resolution and was not rendered.",
                    index,
                    identifier,
                )
                for identifier in ([None] if regions is None else [i for i, _ in boxes])
            )
            return [], findings, 0, drawn
        cropped = PdfiumImageExtractor._crop(pages, index, boxes)
        return cropped, findings, 1 if regions is None else len(cropped), drawn

    @staticmethod
    def _crop(
        pages: PdfPages,
        index: int,
        boxes: list[tuple[str | None, DisplayedBox]],
    ) -> list[ExtractedImage]:
        """Crop every pixel each box touches from one rendering of the page."""
        image, (width, height) = pages.render(index)
        try:
            scale_x, scale_y = image.width / width, image.height / height
            cropped: list[ExtractedImage] = []
            for identifier, (left, top, right, bottom) in boxes:
                x0 = min(max(0, int(left * scale_x)), image.width - 1)
                y0 = min(max(0, int(top * scale_y)), image.height - 1)
                x1 = min(image.width, max(x0 + 1, int(right * scale_x + 0.999)))
                y1 = min(image.height, max(y0 + 1, int(bottom * scale_y + 0.999)))
                with image.crop((x0, y0, x1, y1)) as piece:
                    cropped.append(
                        ExtractedImage(
                            data=png(piece),
                            media_type="image/png",
                            width=piece.width,
                            height=piece.height,
                            page_index=index,
                            region_id=identifier,
                            method="rendered",
                        )
                    )
            return cropped
        finally:
            image.close()

    # Stored mode -------------------------------------------------------------

    def _stored_page(
        self,
        document: Any,
        index: int,
        unit: float,
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> PageOutcome:
        """One page's stored pictures, findings, completed work, and pictures drawn.

        Without a layout, a searched page counts as completed work even when
        nothing is drawn on it; with a layout, each region that was matched does.
        """
        findings: list[ImageExtractFinding] = []
        owned: list[tuple[str | None, Any]] = []
        completed = 0
        with closing(document[index]) as page:
            crop, rotation, size = page_geometry(page, unit)
            pictures = [
                (picture, bounds)
                for picture, bounds in drawn_pictures(page)
                if inside(crop, bounds)
            ]
            if regions is None:
                owned = [(None, picture) for picture, _ in pictures]
                completed = 1
            else:
                areas: list[tuple[LayoutRegion, Any]] = []
                for region in regions:
                    if region.bbox is None or not fits_page(
                        surfaces[region.surface_id], size
                    ):
                        findings.append(mismatch(region, index))
                    else:
                        areas.append(
                            (region, pdf_box(region.bbox, crop, rotation, unit))
                        )
                held, conflicts = claim(
                    [bounds for _, bounds in pictures], [area for _, area in areas]
                )
                claims = [[pictures[n][0] for n in numbers] for numbers in held]
                for number, (region, _) in enumerate(areas):
                    if number in conflicts:
                        findings.append(
                            finding(
                                ImageFindingCode.SELECTION_OVERLAP,
                                "Selected regions claim the same picture; neither "
                                "overlapping region was extracted.",
                                index,
                                region.id,
                            )
                        )
                        continue
                    completed += 1
                    if not claims[number]:
                        findings.append(
                            finding(
                                ImageFindingCode.EMPTY,
                                "No stored picture is drawn in this region; "
                                "rendered mode can crop what is visible there.",
                                index,
                                region.id,
                            )
                        )
                    owned.extend((region.id, picture) for picture in claims[number])
            images: list[ExtractedImage] = []
            for identifier, picture in owned:
                extracted = PdfiumImageExtractor._stored_picture(
                    picture, index, identifier
                )
                if isinstance(extracted, ExtractedImage):
                    images.append(extracted)
                else:
                    findings.append(extracted)
        drawn = len(pictures) if regions is None else 0
        return images, findings, completed, drawn

    @staticmethod
    def _stored_picture(
        picture: Any, index: int, region_id: str | None
    ) -> ExtractedImage | ImageExtractFinding:
        import pypdfium2 as pdfium

        try:
            width, height = picture.get_px_size()
            # PDFium decodes pixels without Pillow's decompression-bomb check,
            # so apply Pillow's own ceiling before decoding.
            if too_many_pixels(width, height):
                return too_large(index, region_id)
            buffer = BytesIO()
            picture.extract(buffer)
            data, media_type, width, height = image_file(buffer.getvalue())
            original = data == bytes(picture.get_data(decode_simple=False))
        except Image.DecompressionBombError:
            return too_large(index, region_id)
        except UnidentifiedImageError:
            return finding(
                ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
                "One picture uses an encoding that cannot be turned into an "
                "image file.",
                index,
                region_id,
            )
        except (pdfium.PdfiumError, OSError, ValueError, TypeError, SyntaxError):
            return finding(
                ImageFindingCode.IMAGE_UNREADABLE,
                "One picture on this page could not be extracted.",
                index,
                region_id,
            )
        return ExtractedImage(
            data=data,
            media_type=media_type,
            width=width,
            height=height,
            page_index=index,
            region_id=region_id,
            method="original" if original else "reencoded",
        )
