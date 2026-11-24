"""Read stored PDF characters with PDFium, by page or by layout region."""

import warnings
from collections.abc import Sequence
from contextlib import closing
from io import BytesIO
from math import isclose
from pathlib import Path
from stat import S_ISREG
from typing import Any
from .geometry import Box, displayed_size, inside, pdf_box, user_unit, visible_crop
from ...shared.pdf_geometry import assemble
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from ...normalization.artifacts import Artifact


class PdfiumTextReader:
    """Read stored characters by page, or by character centres inside regions.

    Matches ``PdfplumberTextReader``'s contract: whole-page reads keep only
    characters whose centre lies in the visible (cropped) area; region reads
    translate ``NativePdfLayoutAnalyzer`` boxes (displayed crop, top-left
    origin, points, rotation, UserUnit) to PDF space and keep characters by
    centre; regions sharing characters are both rejected. Words are separated
    by single spaces and lines by ``\\n``. No OCR; images and annotations are
    out of scope. Passwords come from ``Artifact``.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY})

    def __init__(self, *, password_error: int = 4) -> None:
        # PDFium's error number for a missing or wrong password (FPDF_ERR_PASSWORD).
        if isinstance(password_error, bool) or not isinstance(password_error, int):
            raise TypeError("password_error must be an integer")
        self._password_error = password_error

    @property
    def name(self) -> str:
        return "text_reader_pdfium"

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
                    PdfiumTextReader._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        try:
            import pikepdf
            import pypdfium2 as pdfium
        except ImportError as error:
            raise ImportError(
                "PDFium text reading requires: pip install 'raggae[text-pdf]'"
            ) from error
        password = artifact.password
        try:
            document = pdfium.PdfDocument(data, password=password)
        except pdfium.PdfiumError as error:
            if getattr(error, "err_code", None) == self._password_error:
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
            # PDFium ignores /UserUnit; read it from the same file with pikepdf,
            # silencing its notice about a password an open PDF did not need.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(
                    BytesIO(data), password=password or ""
                ) as metadata:
                    units = [
                        user_unit(page.obj.get("/UserUnit", 1))
                        for page in metadata.pages
                    ]
            if len(units) != len(document):
                raise ValueError("page count mismatch")
        except (pikepdf.PdfError, ValueError, TypeError, KeyError):
            document.close()
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        try:
            if not len(document):
                return self._failure(
                    FindingCode.NO_PAGES, "PDF contains no usable pages."
                )
            surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
            return self._read_document(document, units, selected, surfaces)
        finally:
            document.close()

    def _read_document(
        self,
        document: Any,
        units: list[float],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> TextReadResult:
        import pypdfium2 as pdfium

        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                surface = surfaces[region.surface_id]
                location = surface.location
                if not isinstance(location, PdfPageLocation) or region.kind == "image":
                    findings.append(
                        PdfiumTextReader._finding(
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
                    continue
                if location.page_number > len(document):
                    findings.append(
                        PdfiumTextReader._finding(
                            FindingCode.PAGE_MISSING,
                            "Requested page does not exist in this PDF.",
                            region_id=region.id,
                            page_index=location.page_number - 1,
                        )
                    )
                    continue
                pending.setdefault(location.page_number - 1, []).append(region)
            indexes = sorted(pending)
        else:
            indexes = list(range(len(document)))

        for index in indexes:
            regions = pending.get(index) if selected is not None else None
            try:
                with (
                    closing(document[index]) as page,
                    closing(page.get_textpage()) as text,
                ):
                    page_blocks, page_findings, page_completed = self._read_page(
                        page, text, index, units[index], regions, surfaces
                    )
            except (pdfium.PdfiumError, ValueError, TypeError, OverflowError):
                for region in regions if regions is not None else (None,):
                    findings.append(
                        PdfiumTextReader._finding(
                            FindingCode.PAGE_UNREADABLE,
                            "Stored text on this PDF page could not be read.",
                            region_id=None if region is None else region.id,
                            page_index=index,
                        )
                    )
                continue
            blocks.extend(page_blocks)
            findings.extend(page_findings)
            completed += page_completed

        incomplete = any(
            f.code not in PdfiumTextReader._INFORMATIONAL for f in findings
        )
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _read_page(
        self,
        page: Any,
        text: Any,
        index: int,
        unit: float,
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> tuple[list[TextBlock], list[TextReadFinding], int]:
        rotation = page.get_rotation()
        crop = visible_crop(page.get_mediabox(), page.get_cropbox(), rotation)
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
                        PdfiumTextReader._finding(
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

        characters = [
            (text.get_text_range(position, 1), text.get_charbox(position))
            for position in range(text.count_chars())
        ]
        owners: list[list[int]] = []
        conflicts: set[int] = set()
        for character, box in characters:
            if character.isspace() or not inside(crop, box):
                owners.append([])
                continue
            found = [
                number for number, (_, area) in enumerate(scopes) if inside(area, box)
            ]
            if len(found) > 1:
                conflicts.update(found)
            owners.append(found)

        blocks: list[TextBlock] = []
        completed = 0
        for number, (identifier, _) in enumerate(scopes):
            if number in conflicts:
                findings.append(
                    PdfiumTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Selected regions share source characters; neither "
                        "overlapping region was read.",
                        region_id=identifier,
                        page_index=index,
                    )
                )
                continue
            completed += 1
            content = assemble(characters, owners, number)
            if not content:
                findings.append(
                    PdfiumTextReader._finding(
                        FindingCode.EMPTY,
                        "No stored text found in this area; it may still contain "
                        "visible text in pixels.",
                        region_id=identifier,
                        page_index=index,
                    )
                )
            else:
                blocks.append(
                    TextBlock(text=content, page_index=index, region_id=identifier)
                )
        return blocks, findings, completed

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

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
