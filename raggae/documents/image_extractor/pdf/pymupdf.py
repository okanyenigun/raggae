"""Stored PDF pictures per drawn occurrence with PyMuPDF, standalone or by region.

PyMuPDF is licensed AGPL-3.0 or under an Artifex commercial licence; it is an
opt-in extra (``raggae[text-pymupdf]``), shared with the PyMuPDF text reader.
"""

import warnings
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from PIL import Image, UnidentifiedImageError
from .pdf_regions import claim, fits_page, mismatch, place_regions
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    failure,
    finding,
    finish,
    image_file,
    nothing_selected,
    read_source,
    select_image_regions,
    too_large,
    too_many_pixels,
)
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion, LayoutSurface
from ...normalization.artifacts import Artifact
from ...pdf_geometry import (
    Box,
    displayed_size,
    inside,
    pdf_box,
    user_unit,
    visible_crop,
)
from ...sources import as_artifact

type Placement = tuple[int, Box, int, int]  # xref (0 inline), PDF-space bounds, pixels


class PyMuPdfImageExtractor:
    """Return the stored picture behind each place a picture is drawn, with PyMuPDF.

    Without a layout: one picture for every time a picture is drawn on a page
    with its centre in the visible area, pictures inside forms included, in
    PyMuPDF's order. A picture drawn twice comes back twice; one listed but
    never drawn does not come back. Inline images (written into the drawing
    commands) have no object to extract: pages that draw them get an
    informational ``inline_images_skipped``.

    A stored JPEG comes back byte for byte (``original``). Other pictures come
    back as new files (``reencoded``): PyMuPDF converts CMYK and spot colours to
    RGB and saves PNG; JPEG 2000 is saved as PNG. PyMuPDF applies soft masks
    when asked, so masked pictures come back as PNG with transparency. Pictures
    above Pillow's decompression-bomb ceiling are skipped before decoding.

    With a layout: the pictures whose drawn centre lies inside each requested
    region, with its ``region_id``. Without ``region_ids`` only image regions
    are used; explicit ``region_ids`` are used whatever their kind. A region
    without a picture gets ``empty``; regions claiming the same picture are both
    refused (``selection_overlap``).

    Requires the opt-in ``raggae[text-pymupdf]`` extra (AGPL-3.0/commercial).
    Page geometry and passwords are read with pikepdf. Passwords come from
    ``Artifact``.
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.NOTHING_SELECTED,
            ImageFindingCode.INLINE_IMAGES_SKIPPED,
        }
    )

    @property
    def name(self) -> str:
        return "image_extractor_pymupdf"

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
        data = read_source(artifact, self.name)
        if isinstance(data, ImageExtractResult):
            return data
        if b"%PDF-" not in data[:1024]:
            return ImageExtractResult(
                extractor=self.name,
                outcome="not_applicable",
                findings=(
                    ImageExtractFinding(
                        code=ImageFindingCode.NOT_APPLICABLE,
                        message="Source has no PDF header.",
                    ),
                ),
            )
        try:
            import pymupdf
        except ImportError as error:
            raise ImportError(
                "PyMuPDF image extraction requires: pip install 'raggae[text-pymupdf]' "
                "(PyMuPDF is AGPL-3.0 or Artifex commercial)"
            ) from error
        geometry = PyMuPdfImageExtractor._geometry(data, artifact.password, self.name)
        if isinstance(geometry, ImageExtractResult):
            return geometry
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
            if document.needs_pass and not document.authenticate(
                artifact.password or ""
            ):
                raise ValueError("not authenticated")
            if len(document) != len(geometry):
                raise ValueError("page count mismatch")
        except Exception:  # noqa: BLE001 - MuPDF raises many error types on bad files
            return failure(
                self.name, ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
            )
        with document:
            surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
            return self._extract(document, geometry, selected, surfaces)

    def _extract(
        self,
        document: Any,
        geometry: list[tuple[Box, int, float]],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> ImageExtractResult:
        findings: list[ImageExtractFinding] = []
        if selected is None:
            plan: dict[int, list[LayoutRegion] | None] = dict.fromkeys(
                range(len(document))
            )
        else:
            pending, findings = place_regions(selected, surfaces, len(document))
            plan = dict(sorted(pending.items()))
        images: list[ExtractedImage] = []
        cache: dict[int, ExtractedImage | ImageExtractFinding] = {}
        completed = drawn = 0
        for index, regions in plan.items():
            crop, rotation, unit = geometry[index]
            try:
                placements = [
                    placement
                    for placement in PyMuPdfImageExtractor._placements(document[index])
                    if inside(crop, placement[1])
                ]
            except Exception:  # noqa: BLE001 - MuPDF raises many error types
                findings.extend(
                    finding(
                        ImageFindingCode.PAGE_UNREADABLE,
                        "The pictures on this PDF page could not be read.",
                        index,
                        identifier,
                    )
                    for identifier in (
                        [None] if regions is None else [r.id for r in regions]
                    )
                )
                continue
            if regions is None:
                completed += 1
                drawn += len(placements)
                owned = [(None, placement) for placement in placements]
            else:
                size = displayed_size(crop, rotation, unit)
                areas: list[tuple[LayoutRegion, Box]] = []
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
                    [p[1] for p in placements], [a for _, a in areas]
                )
                owned = []
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
                    if not held[number]:
                        findings.append(
                            finding(
                                ImageFindingCode.EMPTY,
                                "No stored picture is drawn in this region.",
                                index,
                                region.id,
                            )
                        )
                    owned.extend((region.id, placements[n]) for n in held[number])
            inline_noted: set[str | None] = set()
            for region_id, (xref, _, width, height) in owned:
                if xref == 0:
                    if region_id not in inline_noted:
                        inline_noted.add(region_id)
                        findings.append(
                            finding(
                                ImageFindingCode.INLINE_IMAGES_SKIPPED,
                                "Inline images (written into the drawing commands) "
                                "were not extracted.",
                                index,
                                region_id,
                            )
                        )
                    continue
                if too_many_pixels(width, height):
                    findings.append(too_large(index, region_id))
                    continue
                if xref not in cache:
                    cache[xref] = PyMuPdfImageExtractor._stored(document, xref)
                extracted = cache[xref].model_copy(
                    update={"page_index": index, "region_id": region_id}
                )
                if isinstance(extracted, ExtractedImage):
                    images.append(extracted)
                else:
                    findings.append(extracted)
        if selected is None and completed and not drawn:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No pictures are drawn in this PDF. Charts drawn with "
                    "lines need PdfiumImageExtractor and a layout region.",
                )
            )
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _geometry(
        data: bytes, password: str | None, extractor: str
    ) -> list[tuple[Box, int, float]] | ImageExtractResult:
        """Each page's visible crop (PDF space), rotation, and UserUnit, read with pikepdf.

        Opening with pikepdf first also gives reliable password and empty-PDF answers.
        """
        import pikepdf

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(
                    BytesIO(data), password=password or ""
                ) as document:
                    pages = [
                        (
                            visible_crop(
                                page.mediabox,
                                page.cropbox,
                                PyMuPdfImageExtractor._rotation(page.obj),
                            ),
                            PyMuPdfImageExtractor._rotation(page.obj),
                            user_unit(page.obj.get("/UserUnit", 1)),
                        )
                        for page in document.pages
                    ]
        except pikepdf.PasswordError:
            code = (
                ImageFindingCode.INVALID_PASSWORD
                if password
                else ImageFindingCode.PASSWORD_REQUIRED
            )
            return failure(
                extractor, code, "PDF could not be opened with the supplied password."
            )
        except (pikepdf.PdfError, ValueError, TypeError, KeyError, OSError):
            return failure(
                extractor, ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
            )
        if not pages:
            return failure(
                extractor, ImageFindingCode.NO_PAGES, "PDF contains no usable pages."
            )
        return pages

    @staticmethod
    def _rotation(page: Any) -> int:
        """The page's /Rotate, inherited from its page tree when the page has none."""
        node = page
        for _ in range(64):
            if node is None:
                break
            value = node.get("/Rotate")
            if value is not None:
                return int(value) % 360
            node = node.get("/Parent")
        return 0

    @staticmethod
    def _placements(page: Any) -> list[Placement]:
        """Each drawn picture: its xref (0 for inline), PDF-space bounds, and pixel size."""
        import pymupdf

        to_pdf = ~page.transformation_matrix
        placements: list[Placement] = []
        for info in page.get_image_info(xrefs=True):
            bounds = pymupdf.Rect(info["bbox"]) * to_pdf
            placements.append(
                (
                    int(info.get("xref") or 0),
                    (bounds.x0, bounds.y0, bounds.x1, bounds.y1),
                    int(info.get("width") or 0),
                    int(info.get("height") or 0),
                )
            )
        return placements

    @staticmethod
    def _stored(document: Any, xref: int) -> ExtractedImage | ImageExtractFinding:
        """The stored picture an object holds, with its soft mask applied."""
        import pymupdf

        try:
            extracted = document.extract_image(xref)
            if not extracted:
                raise ValueError("not an image")
            if extracted.get("smask"):
                base = pymupdf.Pixmap(document, xref)
                if base.n - base.alpha >= 4:  # CMYK: masks need RGB
                    base = pymupdf.Pixmap(pymupdf.csRGB, base)
                masked = pymupdf.Pixmap(
                    base, pymupdf.Pixmap(document, extracted["smask"])
                )
                data, original = masked.tobytes("png"), False
                data, media_type, width, height = image_file(data)
            else:
                data, media_type, width, height = image_file(extracted["image"])
                original = data == document.xref_stream_raw(xref)
        except Image.DecompressionBombError:
            return too_large(0, None)
        except UnidentifiedImageError:
            return finding(
                ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
                "One picture uses an encoding that cannot be turned into an image file.",
                0,
            )
        except Exception:  # noqa: BLE001 - MuPDF raises many error types
            return finding(
                ImageFindingCode.IMAGE_UNREADABLE,
                "One picture on this page could not be extracted.",
                0,
            )
        return ExtractedImage(
            data=data,
            media_type=media_type,
            width=width,
            height=height,
            method="original" if original else "reencoded",
        )
