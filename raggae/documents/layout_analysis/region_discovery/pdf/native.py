"""Locate native PDF content without rewriting the source or running OCR."""

from pathlib import Path
from io import BytesIO
from stat import S_ISREG
from typing import TYPE_CHECKING, Any
from .policy import PdfLayoutPolicy
from ...models import (
    BoundingBox,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
    RegionKind,
)
from .blocks import text_blocks
from .geometry import contains_center, displayed_crop, local_box
from ...utils import resolve_source
from ....normalization.artifacts import Artifact

if TYPE_CHECKING:
    from pdfplumber.page import Page


class NativePdfLayoutAnalyzer:
    """Find candidate text blocks, raster image placements, and ruled tables.

    Limits bound source size, page processing and post-parse grouping work.
    They do not impose a hard memory/time limit on pdfminer parsing itself;
    hostile PDFs require application-level process isolation.
    """

    def __init__(self, policy: PdfLayoutPolicy | None = None) -> None:
        self._policy = policy if policy is not None else PdfLayoutPolicy()

    @property
    def policy(self) -> PdfLayoutPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_native_pdf"

    def analyze(
        self,
        source: Path | str | bytes | Artifact,
    ) -> LayoutAnalysisResult:
        source = resolve_source(source)
        try:
            if source.path is not None:
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
            else:
                assert source.data is not None
                data = source.data
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure(
                "source_too_large", "Source exceeds the configured byte limit."
            )
        if not data:
            return self._failure("decode_failed", "Source is empty.")
        if b"%PDF-" not in data[:1024]:
            return LayoutAnalysisResult(
                outcome="not_applicable",
                findings=(
                    LayoutFinding(
                        code="layout.pdf.not_applicable",
                        message="Source has no PDF header.",
                    ),
                ),
            )
        return self._analyze_pdf(data, source.password)

    def _analyze_pdf(self, data: bytes, password: str | None) -> LayoutAnalysisResult:
        try:
            import pdfplumber
            from pdfminer.pdfdocument import PDFPasswordIncorrect
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdftypes import PDFException
            from pdfminer.psparser import PSException
            from pdfplumber.page import Page
            from pdfplumber.utils.exceptions import (
                MalformedPDFException,
                PdfminerException,
            )
        except ImportError as error:
            raise ImportError(
                "PDF layout requires: pip install 'raggae[layout-pdf]'"
            ) from error

        parse_errors = (
            PdfminerException,
            MalformedPDFException,
            PDFException,
            PSException,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            RecursionError,
        )
        surfaces: list[LayoutSurface] = []
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []
        with BytesIO(data) as stream:
            try:
                document = pdfplumber.open(
                    stream, password=password, strict_metadata=True
                )
            except parse_errors as error:
                cause = (
                    error.args[0]
                    if isinstance(error, PdfminerException) and error.args
                    else error
                )
                if isinstance(cause, PDFPasswordIncorrect):
                    code = "password_required" if not password else "invalid_password"
                    return self._failure(
                        code, "PDF could not be opened with the supplied password."
                    )
                return self._failure("decode_failed", "PDF could not be opened.")
            try:
                # PDF.pages materializes the entire page tree. Iterate the backend's
                # pages instead, stopping after at most one look-ahead page.
                for number, raw_page in enumerate(
                    PDFPage.create_pages(document.doc), 1
                ):
                    if number > self._policy.max_pages:
                        findings.append(
                            LayoutFinding(
                                code="layout.pdf.page_limit",
                                message="Remaining pages were not analyzed: page limit reached.",
                            )
                        )
                        break
                    page = None
                    try:
                        page = Page(document, raw_page, page_number=number)
                        surface, page_regions, page_findings = self._analyze_page(page)
                    except parse_errors:
                        findings.append(
                            LayoutFinding(
                                code="layout.pdf.page_unreadable",
                                message=f"PDF page {number} could not be analyzed.",
                            )
                        )
                    else:
                        surfaces.append(surface)
                        regions.extend(page_regions)
                        findings.extend(page_findings)
                    finally:
                        if page is not None:
                            page.close()
            except parse_errors:
                findings.append(
                    LayoutFinding(
                        code="layout.pdf.decode_failed",
                        message="PDF page traversal could not finish.",
                    )
                )
            finally:
                # PDF.close() also visits PDF.pages. We own/close the external
                # stream above and close each constructed page, without that traversal.
                document.flush_cache()
        if not surfaces:
            return LayoutAnalysisResult(
                outcome="failed",
                findings=tuple(findings)
                or (
                    LayoutFinding(
                        code="layout.pdf.no_pages",
                        message="PDF contains no usable pages.",
                    ),
                ),
            )
        return LayoutAnalysisResult(
            outcome="partial" if findings else "success",
            surfaces=tuple(surfaces),
            regions=tuple(regions),
            findings=tuple(findings),
        )

    def _analyze_page(
        self,
        page: "Page",
    ) -> tuple[LayoutSurface, list[LayoutRegion], list[LayoutFinding]]:
        crop, unit = displayed_crop(page)
        width, height = (crop[2] - crop[0]) * unit, (crop[3] - crop[1]) * unit
        surface = LayoutSurface(
            id_=f"page-{page.page_number}",
            location=PdfPageLocation(page_number=page.page_number),
            width=width,
            height=height,
        )
        if (
            sum(len(objects) for objects in page.objects.values())
            > self._policy.max_objects_per_page
        ):
            return (
                surface,
                [],
                [
                    LayoutFinding(
                        code="layout.pdf.object_limit",
                        surface_id=surface.id_,
                        message="Page regions were not analyzed: object limit exceeded.",
                    )
                ],
            )
        view = page.crop(crop)
        tables = [table.bbox for table in view.find_tables()]
        candidates: list[tuple[RegionKind, BoundingBox]] = [
            ("table", box) for box in tables
        ]
        candidates.extend(("image", self._box(image)) for image in view.images)

        text_view = view.filter(
            lambda obj: obj["object_type"] != "char"
            or not any(contains_center(table, self._box(obj)) for table in tables)
        )
        horizontal: list[BoundingBox] = []
        vertical: list[BoundingBox] = []
        for word in text_view.extract_words():
            box = self._box(word)
            if word["upright"]:
                horizontal.append(box)
            else:
                vertical.append((box[1], box[0], box[3], box[2]))
        candidates.extend(("text", box) for box in text_blocks(horizontal))
        candidates.extend(
            ("text", (box[1], box[0], box[3], box[2])) for box in text_blocks(vertical)
        )
        located: list[tuple[RegionKind, BoundingBox]] = [
            (kind, local)
            for kind, box in candidates
            if (local := local_box(box, crop, unit)) is not None
        ]
        regions = [
            LayoutRegion(
                id=f"{surface.id_}-r{index}",
                surface_id=surface.id_,
                kind=kind,
                bbox=box,
            )
            for index, (kind, box) in enumerate(
                sorted(located, key=lambda item: (item[1][1], item[1][0], item[0])), 1
            )
        ]
        findings: list[LayoutFinding] = []
        images = [region for region in regions if region.kind == "image"]
        if images and (
            not view.chars
            or any(
                image.bbox is not None
                and (image.bbox[2] - image.bbox[0]) * (image.bbox[3] - image.bbox[1])
                >= width * height * 0.8
                for image in images
            )
        ):
            findings.append(
                LayoutFinding(
                    code="layout.pdf.image_content_unanalyzed",
                    surface_id=surface.id_,
                    message="Image-dominated content may contain text or tables; its internal layout was not analyzed.",
                )
            )
        if any(
            not any(contains_center(table, self._box(shape)) for table in tables)
            for shape in (*view.lines, *view.rects, *view.curves)
        ):
            findings.append(
                LayoutFinding(
                    code="layout.pdf.vector_content_unclassified",
                    surface_id=surface.id_,
                    message="Drawing objects outside detected tables were not classified as regions.",
                )
            )
        if page.page_obj.attrs.get("Annots"):
            findings.append(
                LayoutFinding(
                    code="layout.pdf.annotations_unanalyzed",
                    surface_id=surface.id_,
                    message="Annotation and form appearances were not analyzed.",
                )
            )
        view.close()
        text_view.close()
        return surface, regions, findings

    @staticmethod
    def _box(obj: dict[str, Any]) -> BoundingBox:
        return (
            float(obj["x0"]),
            float(obj["top"]),
            float(obj["x1"]),
            float(obj["bottom"]),
        )

    @staticmethod
    def _failure(code: str, message: str) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            outcome="failed",
            findings=(LayoutFinding(code=f"layout.pdf.{code}", message=message),),
        )
