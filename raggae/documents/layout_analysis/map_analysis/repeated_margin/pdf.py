"""Compare native text from existing PDF regions near the top/bottom margins."""

from pathlib import Path
from io import BytesIO
from math import isclose
from stat import S_ISREG
from typing import Literal

from .policy import RepeatedMarginPolicy
from ....normalization.artifacts import Artifact
from ...utils import resolve_source

from ...models import LayoutAnalysisResult, LayoutFinding, LayoutRegion, LayoutSurface, PdfPageLocation
from ...region_discovery.pdf.geometry import contains_center, displayed_crop, local_box
from .matching import Candidate, group_candidates, signature
from .models import RepeatedMarginResult


class PdfRepeatedMarginDetector:
    """Propose recurring text headers/footers; do not rewrite the map or PDF."""

    def __init__(self, policy: RepeatedMarginPolicy | None = None) -> None:
        self._policy = policy if policy is not None else RepeatedMarginPolicy()

    @property
    def policy(self) -> RepeatedMarginPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_pdf_repeated_margin"

    def detect(self, layout: LayoutAnalysisResult, source: Path | str | bytes | Artifact) -> RepeatedMarginResult:
        if not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult")
        source = resolve_source(source)
        if layout.outcome in ("failed", "not_applicable"):
            return RepeatedMarginResult(outcome=layout.outcome, findings=layout.findings)
        surfaces = {surface.location.page_number: surface for surface in layout.surfaces
                    if surface.kind == "page" and isinstance(surface.location, PdfPageLocation)}
        if len(surfaces) != sum(surface.kind == "page" for surface in layout.surfaces):
            raise ValueError("PDF page locators must be unique")
        if len(surfaces) < self._policy.minimum_pages:
            return RepeatedMarginResult(outcome="not_applicable", findings=layout.findings + (LayoutFinding(
                code="layout.margin.insufficient_pages", message="There are too few PDF pages for repeated-margin comparison.",
            ),))
        findings = list(layout.findings)
        if len(surfaces) != len(layout.surfaces):
            findings.append(LayoutFinding(code="layout.margin.non_pdf_surfaces", message="Non-PDF surfaces were not compared."))
        if any(number > self._policy.max_pages for number in surfaces):
            findings.append(LayoutFinding(code="layout.margin.page_limit", message="PDF pages beyond the traversal limit were not compared."))
        selected: dict[int, list[tuple[LayoutRegion, Literal["header", "footer"]]]] = {}
        surface_numbers = {surface.id_: number for number, surface in surfaces.items()}
        count = 0
        for region in layout.regions:
            number = surface_numbers.get(region.surface_id)
            if number is None or number > self._policy.max_pages or region.bbox is None:
                continue
            height = surfaces[number].height
            assert height is not None
            kind: Literal["header", "footer"]
            if region.bbox[3] <= height * self._policy.margin_ratio:
                kind = "header"
            elif region.bbox[1] >= height * (1 - self._policy.margin_ratio):
                kind = "footer"
            else:
                continue
            if region.kind not in ("text", "heading"):
                findings.append(LayoutFinding(code="layout.margin.non_text_margin", surface_id=region.surface_id, region_id=region.id,
                                              message="A non-text margin region was not compared; no visual/OCR evidence was used."))
                continue
            if count >= self._policy.max_margin_regions:
                findings.append(LayoutFinding(code="layout.margin.candidate_limit", message="Remaining margin regions were not compared: candidate limit reached."))
                break
            selected.setdefault(number, []).append((region, kind))
            count += 1
        if not selected:
            return RepeatedMarginResult(outcome="partial" if findings else "success", findings=tuple(findings))
        try:
            if source.path is not None:
                path = source.path
                info = path.stat()
                if not S_ISREG(info.st_mode):
                    return self._failure("source_unreadable", "Source must be a regular file.")
                if info.st_size > self._policy.max_source_bytes:
                    return self._failure("source_too_large", "Source exceeds the byte limit.")
                with path.open("rb") as stream:
                    data = stream.read(self._policy.max_source_bytes + 1)
            else:
                assert source.data is not None
                data = source.data
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure("source_too_large", "Source exceeds the byte limit.")
        return self._compare(data, source.password, surfaces, selected, findings)

    def _compare(self, data: bytes, password: str | None, surfaces: dict[int, LayoutSurface],
                 selected: dict[int, list[tuple[LayoutRegion, Literal["header", "footer"]]]],
                 findings: list[LayoutFinding]) -> RepeatedMarginResult:
        try:
            import pdfplumber
            from pdfminer.pdfdocument import PDFPasswordIncorrect
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdftypes import PDFException
            from pdfminer.psparser import PSException
            from pdfplumber.page import Page
            from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException
        except ImportError as error:
            raise ImportError("PDF margin detection requires: pip install 'raggae[layout-pdf]'") from error
        errors = (PdfminerException, MalformedPDFException, PDFException, PSException, ValueError,
                  TypeError, KeyError, IndexError, OverflowError, RecursionError)
        candidates: list[Candidate] = []
        visited = set()
        with BytesIO(data) as stream:
            try:
                document = pdfplumber.open(stream, password=password, strict_metadata=True)
            except errors as error:
                cause = error.args[0] if isinstance(error, PdfminerException) and error.args else error
                if isinstance(cause, PDFPasswordIncorrect):
                    return self._failure("invalid_password" if password else "password_required", "PDF could not be opened with the supplied password.")
                return self._failure("decode_failed", "PDF could not be opened.")
            try:
                for number, raw_page in enumerate(PDFPage.create_pages(document.doc), 1):
                    if number > max(selected):
                        break
                    if number not in selected:
                        continue
                    visited.add(number)
                    page = None
                    try:
                        page = Page(document, raw_page, page_number=number)
                        crop, unit = displayed_crop(page)
                        surface = surfaces[number]
                        assert surface.width is not None and surface.height is not None
                        if (not isclose((crop[2]-crop[0])*unit, surface.width, abs_tol=0.01)
                                or not isclose((crop[3]-crop[1])*unit, surface.height, abs_tol=0.01)):
                            return self._failure("source_mismatch", "PDF geometry does not match the supplied layout.")
                        if sum(len(items) for items in page.objects.values()) > self._policy.max_objects_per_page:
                            findings.append(LayoutFinding(code="layout.margin.object_limit", surface_id=surface.id_,
                                                          message="Margin text was not read: page object limit exceeded."))
                            continue
                        words = [(word["text"], box) for word in page.extract_words()
                                 if (box := local_box((word["x0"], word["top"], word["x1"], word["bottom"]), crop, unit)) is not None]
                        for region, kind in selected[number]:
                            assert region.bbox is not None
                            text = " ".join(text for text, box in words if contains_center(region.bbox, box))
                            if not text.strip() or len(text) > 512:
                                findings.append(LayoutFinding(code="layout.margin.text_unavailable", surface_id=surface.id_, region_id=region.id,
                                                              message="Margin region has no usable short native-text sample."))
                                continue
                            key, basis = signature(text, number)
                            box = region.bbox
                            candidates.append(Candidate(region.id, number, kind, key, basis,
                                                        (box[0]+box[2])/(2*surface.width), (box[1]+box[3])/(2*surface.height)))
                    except errors:
                        findings.append(LayoutFinding(code="layout.margin.page_unreadable", surface_id=surfaces[number].id_,
                                                      message="Native margin text could not be read on this page."))
                    finally:
                        if page is not None:
                            page.close()
            except errors:
                findings.append(LayoutFinding(code="layout.margin.decode_failed", message="PDF page traversal could not finish."))
            finally:
                document.flush_cache()
        if set(selected) - visited:
            return self._failure("source_mismatch", "Supplied layout references PDF pages absent from the source.")
        groups, ambiguous = group_candidates(candidates, page_count=len(surfaces), minimum_pages=self._policy.minimum_pages,
                                             minimum_page_fraction=self._policy.minimum_page_fraction, position_tolerance=self._policy.position_tolerance)
        if ambiguous:
            findings.append(LayoutFinding(code="layout.margin.ambiguous_match", message="Repeated same-page candidates were ambiguous; their group was not returned."))
        return RepeatedMarginResult(outcome="partial" if findings else "success", groups=groups, findings=tuple(findings))

    @staticmethod
    def _failure(code: str, message: str) -> RepeatedMarginResult:
        return RepeatedMarginResult(outcome="failed", findings=(LayoutFinding(code=f"layout.margin.{code}", message=message),))
