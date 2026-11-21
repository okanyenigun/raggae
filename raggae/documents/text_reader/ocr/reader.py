"""Recognize text in PDF pages, image frames, or their layout regions."""

import warnings
from collections.abc import Sequence
from io import BytesIO
from math import isclose, isfinite
from pathlib import Path
from stat import S_ISREG
from PIL import Image
from .base import OcrService
from .pages import ImageFrames, Pages, PdfPages
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from ...normalization.artifacts import Artifact


type PixelBox = tuple[int, int, int, int]


class OcrTextReader:
    """Recognize text with an injected ``OcrService``, by page/frame or region.

    PDF pages are rendered with PDFium at ``dpi`` (default 300) from clean page
    content (no annotations); image files are decoded with Pillow, every TIFF
    frame included. Without a layout, each page or frame becomes one block;
    with a layout from ``NativePdfLayoutAnalyzer`` or ``VisualLayoutAnalyzer``,
    each selected region is cropped from its rendered page or decoded frame and
    recognized on its own. Regions whose areas overlap are both rejected.
    Blocks have ``method="ocr"``; ``page_index`` is the page or frame position.
    The reader never skips pages that also hold native text: choosing between
    native and OCR reading belongs to a coordinator.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY})

    def __init__(self, *, service: OcrService, dpi: float = 300) -> None:
        if not isinstance(service, OcrService):
            raise TypeError("service must provide recognize(image) -> str")
        if (
            isinstance(dpi, bool)
            or not isinstance(dpi, (int, float))
            or not isfinite(dpi)
            or dpi <= 0
        ):
            raise ValueError("dpi must be a positive number")
        self._service = service
        self._dpi = float(dpi)

    @property
    def name(self) -> str:
        return "text_reader_ocr"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        artifact = as_artifact(source)
        selected = select_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name)
        try:
            if artifact.path is not None and not S_ISREG(artifact.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = artifact.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(FindingCode.DECODE_FAILED, "Source is empty.")
        surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
        if b"%PDF-" in data[:1024]:
            pages = self._pdf_pages(data, artifact.password)
        else:
            pages = self._image_frames(data)
        if isinstance(pages, TextReadResult):
            return pages
        return self._recognize(pages, selected, surfaces)

    # Sources ---------------------------------------------------------------

    def _pdf_pages(self, data: bytes, password: str | None) -> Pages | TextReadResult:
        try:
            import pikepdf
            import pypdfium2 as pdfium
        except ImportError as error:
            raise ImportError(
                "OCR of PDF pages requires: pip install 'raggae[image-pdf]'"
            ) from error
        try:
            document = pdfium.PdfDocument(data, password=password)
        except pdfium.PdfiumError as error:
            if getattr(error, "err_code", None) == 4:  # FPDF_ERR_PASSWORD
                code = (
                    FindingCode.INVALID_PASSWORD
                    if password
                    else FindingCode.PASSWORD_REQUIRED
                )
                return self._failure(
                    code, "PDF could not be opened with the supplied password."
                )
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(
                    BytesIO(data), password=password or ""
                ) as metadata:
                    units = [
                        float(page.obj.get("/UserUnit", 1)) for page in metadata.pages
                    ]
        except (pikepdf.PdfError, ValueError, TypeError):
            document.close()
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        if len(units) != len(document) or not all(isfinite(u) and u > 0 for u in units):
            document.close()
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        if not len(document):
            document.close()
            return self._failure(FindingCode.NO_PAGES, "PDF contains no usable pages.")
        return PdfPages(document, units, self._dpi)

    def _image_frames(self, data: bytes) -> Pages | TextReadResult:
        try:
            image = Image.open(BytesIO(data))
            image.verify()  # Detects truncated or non-image data without decoding.
            image = Image.open(BytesIO(data))
        except Image.DecompressionBombError:
            return self._failure(
                FindingCode.DECODE_FAILED, "Image exceeds Pillow's pixel limit."
            )
        except (OSError, ValueError, SyntaxError):
            return TextReadResult(
                reader=self.name,
                outcome="not_applicable",
                findings=(
                    OcrTextReader._finding(
                        FindingCode.NOT_APPLICABLE,
                        "Source is neither a PDF nor a supported image.",
                    ),
                ),
            )
        return ImageFrames(image)

    # Recognition -----------------------------------------------------------

    def _recognize(
        self,
        pages: Pages,
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> TextReadResult:
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                index = pages.locate(surfaces[region.surface_id].location)
                if index is None:
                    findings.append(
                        OcrTextReader._finding(
                            FindingCode.SELECTION_UNSUPPORTED,
                            "This region is not on a PDF page or image frame of this source.",
                            region_id=region.id,
                        )
                    )
                elif index >= pages.count:
                    findings.append(
                        OcrTextReader._finding(
                            FindingCode.PAGE_MISSING,
                            "Requested page or frame does not exist in this source.",
                            region_id=region.id,
                            page_index=index,
                        )
                    )
                else:
                    pending.setdefault(index, []).append(region)
        indexes = sorted(pending) if selected is not None else range(pages.count)
        try:
            for index in indexes:
                try:
                    image, size = pages.render(index)
                except (OSError, ValueError, RuntimeError):
                    targets: list[LayoutRegion | None] = (
                        list(pending[index]) if selected is not None else [None]
                    )
                    for region in targets:
                        findings.append(
                            OcrTextReader._finding(
                                FindingCode.PAGE_UNREADABLE,
                                "This page or frame could not be rendered or decoded.",
                                region_id=None if region is None else region.id,
                                page_index=index,
                            )
                        )
                    continue
                with image:
                    scopes = (
                        self._scopes(
                            index, image, size, pending.get(index), surfaces, findings
                        )
                        if selected is not None
                        else [(None, None)]
                    )
                    for identifier, box in scopes:
                        crop = image if box is None else image.crop(box)
                        try:
                            text = self._service.recognize(crop).strip()
                        # The service is injected; any failure is reported per scope.
                        except Exception:  # noqa: BLE001
                            findings.append(
                                OcrTextReader._finding(
                                    FindingCode.OCR_FAILED,
                                    "The OCR service failed on this page or region.",
                                    region_id=identifier,
                                    page_index=index,
                                )
                            )
                            continue
                        finally:
                            if crop is not image:
                                crop.close()
                        completed += 1
                        if text:
                            blocks.append(
                                TextBlock(
                                    text=text,
                                    page_index=index,
                                    region_id=identifier,
                                    method="ocr",
                                )
                            )
                        else:
                            findings.append(
                                OcrTextReader._finding(
                                    FindingCode.EMPTY,
                                    "No text was recognized in this page or region.",
                                    region_id=identifier,
                                    page_index=index,
                                )
                            )
        finally:
            pages.close()
        incomplete = any(f.code not in OcrTextReader._INFORMATIONAL for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _scopes(
        self,
        index: int,
        image: Image.Image,
        size: tuple[float, float],
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
        findings: list[TextReadFinding],
    ) -> list[tuple[str | None, PixelBox | None]]:
        """Pixel crops for this page's regions; overlapping regions are rejected."""
        width, height = size
        scale_x, scale_y = image.width / width, image.height / height
        boxes: list[tuple[str, PixelBox]] = []
        for region in regions or []:
            surface = surfaces[region.surface_id]
            if (
                region.bbox is None
                or surface.width is None
                or surface.height is None
                or not isclose(surface.width, width, abs_tol=0.01, rel_tol=1e-6)
                or not isclose(surface.height, height, abs_tol=0.01, rel_tol=1e-6)
            ):
                findings.append(
                    OcrTextReader._finding(
                        FindingCode.LAYOUT_MISMATCH,
                        "Layout dimensions do not match the source page or frame.",
                        region_id=region.id,
                        page_index=index,
                    )
                )
                continue
            left, top, right, bottom = region.bbox
            box = (
                max(0, int(left * scale_x)),
                max(0, int(top * scale_y)),
                min(image.width, int(right * scale_x + 0.999)),
                min(image.height, int(bottom * scale_y + 0.999)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                findings.append(
                    OcrTextReader._finding(
                        FindingCode.EMPTY,
                        "No text was recognized in this page or region.",
                        region_id=region.id,
                        page_index=index,
                    )
                )
                continue
            boxes.append((region.id, box))
        overlapping = {
            identifier
            for position, (identifier, box) in enumerate(boxes)
            for other_id, other in boxes[position + 1 :]
            if OcrTextReader._overlap(box, other)
            for identifier in (identifier, other_id)
        }
        for identifier, _ in boxes:
            if identifier in overlapping:
                findings.append(
                    OcrTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Selected regions overlap; neither overlapping region was read.",
                        region_id=identifier,
                        page_index=index,
                    )
                )
        return [
            (identifier, box)
            for identifier, box in boxes
            if identifier not in overlapping
        ]

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _overlap(first: PixelBox, second: PixelBox) -> bool:
        return min(first[2], second[2]) > max(first[0], second[0]) and min(
            first[3], second[3]
        ) > max(first[1], second[1])

    @staticmethod
    def _finding(
        code: FindingCode,
        message: str,
        *,
        region_id: str | None = None,
        page_index: int | None = None,
    ) -> TextReadFinding:
        return TextReadFinding(
            code=code,
            message=message,
            region_id=region_id,
            page_index=page_index,
        )
