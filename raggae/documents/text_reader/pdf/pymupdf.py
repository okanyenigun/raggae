"""Read stored PDF text with PyMuPDF, by page or by layout region.

PyMuPDF is licensed AGPL-3.0 or under an Artifex commercial licence; it is
installed only through the opt-in ``raggae[text-pymupdf]`` extra.
"""

import warnings
from collections.abc import Sequence
from io import BytesIO
from math import isclose
from pathlib import Path
from stat import S_ISREG
from typing import Any
from .geometry import Box, displayed_size, inside, pdf_box, user_unit, visible_crop
from ..models import (
    FindingCode,
    TextBlock,
    TextReadFinding,
    TextReadMethod,
    TextReadResult,
)
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from ...normalization.artifacts import Artifact


class PyMuPdfTextReader:
    """Read stored characters with PyMuPDF, by page or by character centres in regions.

    Same contract as ``PdfplumberTextReader``, ``PdfiumTextReader``, and
    ``PdfminerTextReader``: whole-page reads keep characters whose centre lies
    in the visible (cropped) area; region reads translate
    ``NativePdfLayoutAnalyzer`` boxes to PDF space and keep characters by
    centre; regions sharing characters are both rejected. Within each page or
    region block, PyMuPDF's text blocks are separated by a blank line and
    lines by ``\\n``. No OCR; images and annotations are out of scope.
    Requires the opt-in ``raggae[text-pymupdf]`` extra (AGPL-3.0/commercial).
    """

    _method: TextReadMethod = "native"
    _empty_message = (
        "No stored text found in this area; it may still contain visible text "
        "in pixels."
    )
    _unreadable_message = "Stored text on this PDF page could not be read."

    @property
    def name(self) -> str:
        return "text_reader_pymupdf"

    def _accepts(self, region: LayoutRegion) -> bool:
        """Whether a region on a PDF page can be read (image regions hold no text)."""
        return region.kind != "image"

    def _raw_blocks(self, page: Any, unit: float) -> list[dict[str, Any]]:
        """PyMuPDF ``rawdict`` blocks in unrotated page space (crop top-left, y
        down), multiplied by ``/UserUnit`` (``unit``) as PyMuPDF reports them."""
        return page.get_text("rawdict")["blocks"]

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
        try:
            import pikepdf
            import pymupdf
        except ImportError as error:
            raise ImportError(
                "PyMuPDF text reading requires: pip install 'raggae[text-pymupdf]' "
                "(PyMuPDF is AGPL-3.0 or Artifex commercial)"
            ) from error
        password = artifact.password
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError, ValueError):
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        try:
            if document.needs_pass and not document.authenticate(password or ""):
                code = (
                    FindingCode.INVALID_PASSWORD
                    if password
                    else FindingCode.PASSWORD_REQUIRED
                )
                return self._failure(
                    code, "PDF could not be opened with the supplied password."
                )
            try:
                # Raw page boxes and /UserUnit, read from the same file.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with pikepdf.Pdf.open(
                        BytesIO(data), password=password or ""
                    ) as metadata:
                        geometry = [
                            (
                                list(page.mediabox),
                                list(page.cropbox),
                                user_unit(page.obj.get("/UserUnit", 1)),
                            )
                            for page in metadata.pages
                        ]
            except (pikepdf.PdfError, ValueError, TypeError, KeyError):
                return self._failure(
                    FindingCode.DECODE_FAILED, "PDF could not be opened."
                )
            if len(geometry) != document.page_count:
                return self._failure(
                    FindingCode.DECODE_FAILED, "PDF could not be opened."
                )
            if not document.page_count:
                return self._failure(
                    FindingCode.NO_PAGES, "PDF contains no usable pages."
                )
            surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
            return self._read_document(document, geometry, selected, surfaces)
        finally:
            document.close()

    def _read_document(
        self,
        document: Any,
        geometry: list[tuple[list[Any], list[Any], float]],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> TextReadResult:
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                location = surfaces[region.surface_id].location
                if not isinstance(location, PdfPageLocation) or not self._accepts(
                    region
                ):
                    findings.append(
                        self._finding(
                            FindingCode.SELECTION_UNSUPPORTED,
                            "Selection is not a native-text PDF region.",
                            region_id=region.id,
                            page_index=(
                                location.page_number - 1
                                if isinstance(location, PdfPageLocation)
                                else None
                            ),
                        )
                    )
                elif location.page_number > document.page_count:
                    findings.append(
                        self._finding(
                            FindingCode.PAGE_MISSING,
                            "Requested page does not exist in this PDF.",
                            region_id=region.id,
                            page_index=location.page_number - 1,
                        )
                    )
                else:
                    pending.setdefault(location.page_number - 1, []).append(region)
            indexes = sorted(pending)
        else:
            indexes = list(range(document.page_count))

        for index in indexes:
            regions = pending.get(index) if selected is not None else None
            try:
                page_blocks, page_findings, page_completed = self._read_page(
                    document[index], index, geometry[index], regions, surfaces
                )
            except (RuntimeError, ValueError, TypeError, KeyError, IndexError):
                for region in regions if regions is not None else (None,):
                    findings.append(
                        self._finding(
                            FindingCode.PAGE_UNREADABLE,
                            self._unreadable_message,
                            region_id=None if region is None else region.id,
                            page_index=index,
                        )
                    )
                continue
            blocks.extend(page_blocks)
            findings.extend(page_findings)
            completed += page_completed

        empty = FindingCode.EMPTY
        incomplete = any(f.code != empty for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _read_page(
        self,
        page: Any,
        index: int,
        geometry: tuple[list[Any], list[Any], float],
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> tuple[list[TextBlock], list[TextReadFinding], int]:
        media, crop_box, unit = geometry
        rotation = page.rotation % 360
        crop = visible_crop(media, crop_box, rotation)
        width, height = displayed_size(crop, rotation, unit)
        findings: list[TextReadFinding] = []
        scopes: list[tuple[str | None, Box]] = []
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
                            page_index=index,
                        )
                    )
                    continue
                scopes.append((region.id, pdf_box(region.bbox, crop, rotation, unit)))
        if not scopes:
            return [], findings, 0

        # PyMuPDF boxes: unrotated, origin at the visible crop's top-left, y down,
        # and (unlike PDFium and pdfminer) already multiplied by /UserUnit.
        left, top = crop[0], crop[3]

        def to_pdf(bbox: Any) -> Box:
            x0, y0, x1, y1 = (value / unit for value in bbox)
            return left + x0, top - y1, left + x1, top - y0

        # Text blocks -> lines -> characters, each owned by at most one scope.
        structure: list[list[list[tuple[str, list[int]]]]] = []
        conflicts: set[int] = set()
        for block in self._raw_blocks(page, unit):
            if block.get("type") != 0:
                continue  # Image blocks carry no stored text.
            lines: list[list[tuple[str, list[int]]]] = []
            for line in block["lines"]:
                characters: list[tuple[str, list[int]]] = []
                for span in line["spans"]:
                    for char in span["chars"]:
                        text, box = char["c"], to_pdf(char["bbox"])
                        if text.isspace() or not inside(crop, box):
                            characters.append((text, []))
                            continue
                        owners = [
                            n for n, (_, area) in enumerate(scopes) if inside(area, box)
                        ]
                        if len(owners) > 1:
                            conflicts.update(owners)
                        characters.append((text, owners))
                lines.append(characters)
            structure.append(lines)

        blocks: list[TextBlock] = []
        completed = 0
        for number, (identifier, _) in enumerate(scopes):
            if number in conflicts:
                findings.append(
                    self._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Selected regions share source characters; neither "
                        "overlapping region was read.",
                        region_id=identifier,
                        page_index=index,
                    )
                )
                continue
            completed += 1
            content = "\n\n".join(
                text
                for lines in structure
                if (
                    text := "\n".join(
                        line
                        for characters in lines
                        if (line := PyMuPdfTextReader._line_text(characters, number))
                    )
                )
            )
            if not content:
                findings.append(
                    self._finding(
                        FindingCode.EMPTY,
                        self._empty_message,
                        region_id=identifier,
                        page_index=index,
                    )
                )
            else:
                blocks.append(
                    TextBlock(
                        text=content,
                        page_index=index,
                        region_id=identifier,
                        method=self._method,
                    )
                )
        return blocks, findings, completed

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    def _finding(
        self,
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

    @staticmethod
    def _line_text(characters: list[tuple[str, list[int]]], scope: int) -> str:
        """A scope's characters on one line; gaps become a single space."""
        pieces: list[str] = []
        gap = False
        for text, owners in characters:
            if scope in owners:
                if pieces and gap:
                    pieces.append(" ")
                pieces.append(text)
                gap = False
            else:
                gap = True
        return "".join(pieces)
