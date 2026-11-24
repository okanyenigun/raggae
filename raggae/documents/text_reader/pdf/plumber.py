"""Read stored PDF characters without OCR, normalization, or hidden routing."""

from collections.abc import Sequence
from io import BytesIO
from math import isclose
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Any
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import nothing_selected
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from ...layout_analysis.region_discovery.pdf.geometry import (
    contains_center,
    displayed_crop,
)
from ...normalization.artifacts import Artifact
from ...shared.pdf_geometry import unrotated_characters


if TYPE_CHECKING:
    from pdfplumber.page import Page


class PdfplumberTextReader:
    """Read native text by page or by character centers inside layout regions.

    Coordinates match NativePdfLayoutAnalyzer: displayed crop, top-left origin,
    points, including rotation and UserUnit. Page/region order is an extraction
    sequence, not a promise of semantic reading order. Images and annotation
    appearances are outside this worker's stored-character scope.
    """

    @property
    def name(self) -> str:
        return "text_reader_pdfplumber"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        if isinstance(source, bytes):
            source = Artifact(data=source)
        elif isinstance(source, (Path, str)):
            source = Artifact(path=source)
        elif not isinstance(source, Artifact):
            raise TypeError("source must be a path, bytes, or Artifact")
        selected = self._selection(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name)
        try:
            if source.path is not None:
                info = source.path.stat()
                if not S_ISREG(info.st_mode):
                    return self._failure(
                        FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                    )
            data = source.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(FindingCode.DECODE_FAILED, "Source is empty.")
        if b"%PDF-" not in data[:1024]:
            return TextReadResult(
                reader=self.name,
                outcome="not_applicable",
                findings=(
                    self._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        return self._read_pdf(data, source.password, layout, selected)

    @staticmethod
    def _selection(
        layout: LayoutAnalysisResult | None,
        region_ids: Sequence[str] | None,
    ) -> tuple[LayoutRegion, ...] | None:
        if layout is not None and not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult or None")
        if region_ids is not None:
            if layout is None:
                raise ValueError("region_ids require a layout")
            if isinstance(region_ids, (str, bytes)) or not isinstance(
                region_ids, Sequence
            ):
                raise TypeError("region_ids must be a sequence of strings")
            if any(not isinstance(identifier, str) for identifier in region_ids):
                raise TypeError("region_ids must contain only strings")
            if len(set(region_ids)) != len(region_ids):
                raise ValueError("region_ids must not contain duplicates")
        if layout is None:
            return None
        if layout.outcome not in ("success", "partial"):
            raise ValueError("layout must contain a usable map")
        regions = {region.id: region for region in layout.regions}
        ids = tuple(regions) if region_ids is None else tuple(region_ids)
        if set(ids) - regions.keys():
            raise ValueError("region_ids must belong to the supplied layout")
        return tuple(regions[identifier] for identifier in ids)

    def _read_pdf(
        self,
        data: bytes,
        password: str | None,
        layout: LayoutAnalysisResult | None,
        selected: tuple[LayoutRegion, ...] | None,
    ) -> TextReadResult:
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
                "PDF text reading requires: pip install 'raggae[text-pdf]'"
            ) from error
        parse_errors = (
            PDFException,
            PSException,
            PdfminerException,
            MalformedPDFException,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            RecursionError,
            OSError,
        )
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        surfaces: dict[str, LayoutSurface] = (
            {} if layout is None else {s.id_: s for s in layout.surfaces}
        )
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                surface = surfaces[region.surface_id]
                if (
                    not isinstance(surface.location, PdfPageLocation)
                    or region.kind == "image"
                ):
                    findings.append(
                        self._finding(
                            FindingCode.SELECTION_UNSUPPORTED,
                            "Selection is not a native-text PDF region.",
                            region_id=region.id,
                            page_index=(
                                surface.location.page_number - 1
                                if isinstance(surface.location, PdfPageLocation)
                                else None
                            ),
                        )
                    )
                    continue
                number = surface.location.page_number
                pending.setdefault(number, []).append(region)
            if not pending:
                return TextReadResult(
                    reader=self.name, outcome="failed", findings=tuple(findings)
                )

        pending_code = FindingCode.PAGE_MISSING
        pending_message = "Requested page does not exist in this PDF."
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
                    code = (
                        FindingCode.PASSWORD_REQUIRED
                        if not password
                        else FindingCode.INVALID_PASSWORD
                    )
                    return self._failure(
                        code, "PDF could not be opened with the supplied password."
                    )
                return self._failure(
                    FindingCode.DECODE_FAILED, "PDF could not be opened."
                )
            try:
                # Parse only requested pages and release each page promptly.
                # PDF.pages would materialize every page, including during close.
                for number, raw_page in enumerate(
                    PDFPage.create_pages(document.doc), 1
                ):
                    if selected is not None and number not in pending:
                        continue
                    regions = pending.pop(number) if selected is not None else None
                    page = None
                    try:
                        page = Page(document, raw_page, page_number=number)
                        page_blocks, page_findings, page_completed = self._read_page(
                            page,
                            regions,
                            surfaces,
                        )
                    except parse_errors:
                        for region in regions if regions is not None else (None,):
                            findings.append(
                                self._finding(
                                    FindingCode.PAGE_UNREADABLE,
                                    "Stored text on this PDF page could not be read.",
                                    region_id=None if region is None else region.id,
                                    page_index=number - 1,
                                )
                            )
                    else:
                        blocks.extend(page_blocks)
                        findings.extend(page_findings)
                        completed += page_completed
                    finally:
                        if page is not None:
                            page.close()
                    if selected is not None and not pending:
                        break
            except parse_errors:
                pending_code = FindingCode.DECODE_FAILED
                pending_message = "PDF page traversal could not finish."
                findings.append(self._finding(pending_code, pending_message))
            finally:
                document.flush_cache()

        for number, regions in pending.items():
            for region in regions:
                findings.append(
                    self._finding(
                        pending_code,
                        pending_message,
                        region_id=region.id,
                        page_index=number - 1,
                    )
                )
        if not completed and not findings:
            findings.append(
                self._finding(FindingCode.NO_PAGES, "PDF contains no usable pages.")
            )
        incomplete = any(f.code != FindingCode.EMPTY for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _read_page(
        self,
        page: "Page",
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> tuple[list[TextBlock], list[TextReadFinding], int]:
        from pdfplumber.utils.text import extract_text

        crop, unit = displayed_crop(page)
        width, height = (crop[2] - crop[0]) * unit, (crop[3] - crop[1]) * unit
        page_index = page.page_number - 1
        scopes: list[tuple[str | None, tuple[float, float, float, float]]] = []
        findings: list[TextReadFinding] = []
        if regions is None:
            scopes.append((None, crop))
        else:
            for region in regions:
                surface = surfaces[region.surface_id]
                if (
                    region.bbox is None
                    or surface.width is None
                    or surface.height is None
                    or not isclose(surface.width, width, abs_tol=0.01, rel_tol=1e-6)
                    or not isclose(surface.height, height, abs_tol=0.01, rel_tol=1e-6)
                ):
                    findings.append(
                        self._finding(
                            FindingCode.LAYOUT_MISMATCH,
                            "Layout dimensions do not match the source page.",
                            region_id=region.id,
                            page_index=page_index,
                        )
                    )
                    continue
                left, top, right, bottom = region.bbox
                backend_box = (
                    crop[0] + left / unit,
                    crop[1] + top / unit,
                    crop[0] + right / unit,
                    crop[1] + bottom / unit,
                )
                scopes.append((region.id, backend_box))
        if not scopes:
            return [], findings, 0
        chars = page.chars
        assignments: list[list[dict[str, Any]]] = [[] for _ in scopes]
        conflicts: set[int] = set()
        for char in chars:
            box = (
                float(char["x0"]),
                float(char["top"]),
                float(char["x1"]),
                float(char["bottom"]),
            )
            if not contains_center(crop, box):
                continue
            owners = [
                index
                for index, (_, area) in enumerate(scopes)
                if contains_center(area, box)
            ]
            if len(owners) > 1:
                conflicts.update(owners)
            elif owners:
                assignments[owners[0]].append(char)

        blocks: list[TextBlock] = []
        completed = 0
        for index, (identifier, _) in enumerate(scopes):
            if index in conflicts:
                findings.append(
                    self._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Selected regions share source characters; neither overlapping region was read.",
                        region_id=identifier,
                        page_index=page_index,
                    )
                )
                continue
            text = extract_text(unrotated_characters(assignments[index], page.rotation))
            completed += 1
            if not text:
                findings.append(
                    self._finding(
                        FindingCode.EMPTY,
                        "No stored text found in this area; it may still contain visible text in pixels.",
                        region_id=identifier,
                        page_index=page_index,
                    )
                )
                continue
            blocks.append(
                TextBlock(text=text, page_index=page_index, region_id=identifier)
            )
        return blocks, findings, completed

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

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return TextReadResult(
            reader=self.name,
            outcome="failed",
            findings=(self._finding(code, message),),
        )
