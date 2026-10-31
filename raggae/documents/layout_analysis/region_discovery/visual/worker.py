"""Render files and locate regions through an injected visual detection service."""

import pikepdf
from contextlib import closing
from pathlib import Path
from io import BytesIO
from math import ceil, isfinite
from stat import S_ISREG
from PIL import Image, UnidentifiedImageError
from .base import LayoutDetectionService
from .policy import VisualLayoutPolicy
from ...models import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
    LayoutOutcome,
)
from ...utils import resolve_source
from ....normalization.artifacts import Artifact


class VisualLayoutAnalyzer:
    """Locate model-predicted regions in PDFs and raster images, without OCR.

    Source/rendering limits do not impose a hard parser or inference time limit.
    The caller owns the service and may reuse it between sequential calls.
    """

    def __init__(
        self,
        *,
        service: LayoutDetectionService,
        policy: VisualLayoutPolicy | None = None,
    ) -> None:
        if not isinstance(service, LayoutDetectionService):
            raise TypeError("service must implement detect(image)")
        self._service = service
        self._policy = policy if policy is not None else VisualLayoutPolicy()

    @property
    def policy(self) -> VisualLayoutPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_visual"

    def analyze(
        self,
        source: Path | str | bytes | Artifact,
    ) -> LayoutAnalysisResult:
        source = resolve_source(source)
        try:
            if source.path is None:
                assert source.data is not None
                data = source.data
            else:
                info = source.path.stat()
                if not S_ISREG(info.st_mode):
                    return self._failure(
                        "source_unreadable", "Source must be a regular file."
                    )
                if info.st_size > self._policy.max_source_bytes:
                    return self._failure(
                        "source_too_large", "Source exceeds the configured byte limit."
                    )
                with source.path.open("rb") as stream:
                    data = stream.read(self._policy.max_source_bytes + 1)
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure(
                "source_too_large", "Source exceeds the configured byte limit."
            )
        if not data:
            return self._failure("decode_failed", "Source is empty.")
        if b"%PDF-" in data[:1024]:
            return self._pdf(data, source.password)
        return self._images(data)

    def _detect(
        self,
        image: Image.Image,
        surface: LayoutSurface,
    ) -> tuple[list[LayoutRegion], list[LayoutFinding]]:
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []
        assert surface.width is not None and surface.height is not None
        sx, sy = surface.width / image.width, surface.height / image.height
        try:
            detections = self._service.detect(image)
            for index, detection in enumerate(detections):
                if index >= self._policy.max_regions_per_page:
                    findings.append(
                        self._finding(
                            "region_limit",
                            "Remaining detections were omitted: region limit reached.",
                            surface.id_,
                        )
                    )
                    break
                left, top, right, bottom = detection.bbox
                clipped = (
                    max(0.0, left),
                    max(0.0, top),
                    min(float(image.width), right),
                    min(float(image.height), bottom),
                )
                if clipped != detection.bbox:
                    findings.append(
                        self._finding(
                            "box_clipped",
                            "A model box extended outside the visible surface.",
                            surface.id_,
                        )
                    )
                if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                    continue
                box = (
                    clipped[0] * sx,
                    clipped[1] * sy,
                    min(surface.width, clipped[2] * sx),
                    min(surface.height, clipped[3] * sy),
                )
                region = LayoutRegion(
                    id=f"{surface.id_}-r{index + 1}",
                    surface_id=surface.id_,
                    kind=detection.kind,
                    bbox=box,
                )
                regions.append(region)
                if detection.kind == "unknown":
                    findings.append(
                        LayoutFinding(
                            code="layout.visual.unknown_label",
                            message="Model label has no equivalent in the region catalog.",
                            surface_id=surface.id_,
                            region_id=region.id,
                        )
                    )
        except Exception:
            # External inference is a failure boundary. Never include model
            # diagnostics (which may contain paths/content) or keep half a page.
            return [], [
                self._finding(
                    "inference_failed",
                    "The detection service could not analyze this surface.",
                    surface.id_,
                )
            ]
        if not regions:
            findings.append(
                self._finding(
                    "no_regions",
                    "No regions detected; this does not establish that the surface is blank.",
                    surface.id_,
                )
            )
        return regions, findings

    def _images(self, data: bytes) -> LayoutAnalysisResult:
        surfaces: list[LayoutSurface] = []
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []
        try:
            with BytesIO(data) as stream, Image.open(stream) as document:
                if document.format not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP", "GIF"}:
                    return self._failure(
                        "not_applicable",
                        "Image format is not supported.",
                        "not_applicable",
                    )
                # Seek one frame at a time, avoiding an eager full TIFF traversal.
                for index in range(self._policy.max_pages + 1):
                    try:
                        document.seek(index)
                    except EOFError:
                        break
                    if index == self._policy.max_pages:
                        findings.append(
                            self._finding(
                                "page_limit",
                                "Remaining image frames were not analyzed: page limit reached.",
                            )
                        )
                        break
                    if (
                        document.width * document.height
                        > self._policy.max_pixels_per_page
                    ):
                        findings.append(
                            self._finding(
                                "pixel_limit",
                                f"Image frame {index + 1} exceeds the pixel limit.",
                            )
                        )
                        continue
                    surface = LayoutSurface(
                        id_=f"image-{index + 1}",
                        kind="image",
                        location=ImageFrameLocation(frame_number=index + 1),
                        width=document.width,
                        height=document.height,
                        unit="px",
                    )
                    orientation = document.getexif().get(274, 1)
                    # Composite transparency on white, as with rendered PDF paper.
                    with (
                        document.convert("RGBA") as rgba,
                        Image.new("RGB", document.size, "white") as rgb,
                    ):
                        with rgba.getchannel("A") as alpha:
                            rgb.paste(rgba, mask=alpha)
                        page_regions, page_findings = self._detect(rgb, surface)
                    surfaces.append(surface)
                    regions.extend(page_regions)
                    findings.extend(page_findings)
                    if orientation != 1:
                        if document.format == "TIFF":
                            findings.append(
                                self._finding(
                                    "decoded_orientation",
                                    "Coordinates follow Pillow's decoded TIFF pixels, including its automatic orientation handling.",
                                    surface.id_,
                                )
                            )
                        else:
                            findings.append(
                                self._finding(
                                    "stored_orientation",
                                    "Coordinates follow stored pixels; EXIF display rotation was not applied.",
                                    surface.id_,
                                )
                            )
        except UnidentifiedImageError:
            return self._failure(
                "not_applicable",
                "Source is neither a readable PDF nor a supported raster image.",
                "not_applicable",
            )
        except (
            OSError,
            ValueError,
            TypeError,
            SyntaxError,
            Image.DecompressionBombError,
        ):
            findings.append(
                self._finding("decode_failed", "Image decoding could not finish.")
            )
        return self._result(surfaces, regions, findings)

    def _pdf(self, data: bytes, password: str | None) -> LayoutAnalysisResult:
        try:
            import pypdfium2 as pdfium
        except ImportError as error:
            raise ImportError(
                "Visual PDF rendering requires: pip install 'raggae[layout-visual]'"
            ) from error
        surfaces: list[LayoutSurface] = []
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []
        try:
            # QPDF supplies UserUnit, which PDFium's public API does not expose.
            with pikepdf.Pdf.open(BytesIO(data), password=password or "") as metadata:
                with pdfium.PdfDocument(data, password=password) as document:
                    for index in range(min(len(document), self._policy.max_pages)):
                        try:
                            with closing(document[index]) as page:
                                unit = float(
                                    metadata.pages[index].obj.get("/UserUnit", 1)
                                )
                                width, height = page.get_size()
                                if not all(
                                    isfinite(v) and v > 0 for v in (unit, width, height)
                                ):
                                    raise ValueError("unsupported page dimensions")
                                scale = self._policy.dpi / 72 * unit
                                if (
                                    ceil(width * scale) * ceil(height * scale)
                                    > self._policy.max_pixels_per_page
                                ):
                                    findings.append(
                                        self._finding(
                                            "pixel_limit",
                                            f"PDF page {index + 1} exceeds the rendered pixel limit.",
                                        )
                                    )
                                    continue
                                surface = LayoutSurface(
                                    id_=f"page-{index + 1}",
                                    location=PdfPageLocation(page_number=index + 1),
                                    width=width * unit,
                                    height=height * unit,
                                    unit="pt",
                                )
                                # Never initialize PDF form actions or JavaScript.
                                # PDFium accepts floating-point scales; its untyped
                                # default of 1 is incorrectly inferred as int.
                                with closing(
                                    page.render(
                                        scale=scale,  # pyright: ignore[reportArgumentType]
                                        draw_annots=True,
                                        may_draw_forms=False,
                                    )
                                ) as bitmap:
                                    with (
                                        bitmap.to_pil() as rendered,
                                        rendered.convert("RGB") as rgb,
                                    ):
                                        page_regions, page_findings = self._detect(
                                            rgb, surface
                                        )
                                surfaces.append(surface)
                                regions.extend(page_regions)
                                findings.extend(page_findings)
                                if metadata.pages[index].obj.get("/Annots"):
                                    findings.append(
                                        self._finding(
                                            "annotation_coverage",
                                            "Static annotation appearances are rendered; interactive form state is not evaluated.",
                                            surface.id_,
                                        )
                                    )
                        except (
                            pdfium.PdfiumError,
                            pikepdf.PdfError,
                            ValueError,
                            TypeError,
                            IndexError,
                            OverflowError,
                        ):
                            findings.append(
                                self._finding(
                                    "page_unreadable",
                                    f"PDF page {index + 1} could not be rendered.",
                                )
                            )
                    if len(document) > self._policy.max_pages:
                        findings.append(
                            self._finding(
                                "page_limit",
                                "Remaining PDF pages were not analyzed: page limit reached.",
                            )
                        )
        except pikepdf.PasswordError:
            return self._failure(
                "invalid_password" if password else "password_required",
                "PDF could not be opened with the supplied password.",
            )
        except (pikepdf.PdfError, pdfium.PdfiumError, ValueError, OSError):
            findings.append(
                self._finding("decode_failed", "PDF decoding could not finish.")
            )
        return self._result(surfaces, regions, findings)

    @staticmethod
    def _finding(
        code: str, message: str, surface_id: str | None = None
    ) -> LayoutFinding:
        return LayoutFinding(
            code=f"layout.visual.{code}", message=message, surface_id=surface_id
        )

    @classmethod
    def _failure(
        cls, code: str, message: str, outcome: LayoutOutcome = "failed"
    ) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            outcome=outcome, findings=(cls._finding(code, message),)
        )

    @classmethod
    def _result(
        cls,
        surfaces: list[LayoutSurface],
        regions: list[LayoutRegion],
        findings: list[LayoutFinding],
    ) -> LayoutAnalysisResult:
        if not surfaces:
            return LayoutAnalysisResult(
                outcome="failed",
                findings=tuple(findings)
                or (
                    cls._finding("no_surfaces", "Source contains no usable surfaces."),
                ),
            )
        return LayoutAnalysisResult(
            outcome="partial" if findings else "success",
            surfaces=tuple(surfaces),
            regions=tuple(regions),
            findings=tuple(findings),
        )
