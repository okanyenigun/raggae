"""Read stored PDF text with pdfminer.six's layout analysis, by page or region."""

from collections.abc import Iterator, Sequence
from io import BytesIO
from math import isclose
from pathlib import Path
from stat import S_ISREG
from typing import Any
from .geometry import Box, displayed_size, inside, pdf_box, user_unit, visible_crop
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from ...normalization.artifacts import Artifact


class PdfminerTextReader:
    """Group stored characters into pdfminer text boxes, by page or by region.

    Characters are selected first, by centre: whole-page reads keep those in
    the visible (cropped) area; region reads translate ``NativePdfLayoutAnalyzer``
    boxes to PDF space, and regions sharing characters are both rejected, as in
    ``PdfplumberTextReader``. pdfminer's layout analysis (default ``LAParams``)
    then groups only the selected characters into lines and text boxes. Each
    block holds one page or region; text boxes are separated by a blank line.
    No OCR; images and annotations are out of scope.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY})

    @property
    def name(self) -> str:
        return "text_reader_pdfminer"

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
                    PdfminerTextReader._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        try:
            from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdfparser import PDFParser
        except ImportError as error:
            raise ImportError(
                "pdfminer text reading requires: pip install 'raggae[text-pdf]'"
            ) from error
        password = artifact.password
        errors = PdfminerTextReader._backend_errors()
        try:
            document = PDFDocument(PDFParser(BytesIO(data)), password=password or "")
            pages = list(PDFPage.create_pages(document))
        except PDFPasswordIncorrect:
            code = (
                FindingCode.INVALID_PASSWORD
                if password
                else FindingCode.PASSWORD_REQUIRED
            )
            return self._failure(
                code, "PDF could not be opened with the supplied password."
            )
        except errors:
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        if not pages:
            return self._failure(FindingCode.NO_PAGES, "PDF contains no usable pages.")
        surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
        return self._read_pages(pages, selected, surfaces, errors)

    def _read_pages(
        self,
        pages: list[Any],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
        errors: tuple[type[BaseException], ...],
    ) -> TextReadResult:
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                location = surfaces[region.surface_id].location
                if not isinstance(location, PdfPageLocation) or region.kind == "image":
                    findings.append(
                        PdfminerTextReader._finding(
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
                elif location.page_number > len(pages):
                    findings.append(
                        PdfminerTextReader._finding(
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
            indexes = list(range(len(pages)))

        for index in indexes:
            regions = pending.get(index) if selected is not None else None
            try:
                page_blocks, page_findings, page_completed = self._read_page(
                    pages[index], index, regions, surfaces
                )
            except errors:
                for region in regions if regions is not None else (None,):
                    findings.append(
                        PdfminerTextReader._finding(
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
            f.code not in PdfminerTextReader._INFORMATIONAL for f in findings
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
        index: int,
        regions: list[LayoutRegion] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> tuple[list[TextBlock], list[TextReadFinding], int]:
        from pdfminer.converter import PDFPageAggregator
        from pdfminer.layout import LAParams, LTChar, LTPage, LTTextContainer
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdftypes import resolve1

        rotation = page.rotate % 360
        crop = visible_crop(page.mediabox, page.cropbox, rotation)
        unit = user_unit(resolve1(page.attrs.get("UserUnit", 1)))
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
                        PdfminerTextReader._finding(
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

        # Raw characters (no layout analysis yet) in pdfminer's device space.
        resources = PDFResourceManager()
        device = PDFPageAggregator(resources, laparams=None)
        PDFPageInterpreter(resources, device).process_page(page)
        rendered = device.get_result()
        to_pdf = PdfminerTextReader._device_to_pdf(page.mediabox, rotation)
        chosen: list[list[Any]] = [[] for _ in scopes]
        conflicts: set[int] = set()
        for character in PdfminerTextReader._characters(rendered, LTChar):
            box = to_pdf(character.bbox)
            # Group in PDF space, where text runs as written; on a rotated page
            # the displayed space would make pdfminer stack letters vertically.
            character.set_bbox(box)
            if not inside(crop, box):
                continue
            owners = [n for n, (_, area) in enumerate(scopes) if inside(area, box)]
            if len(owners) > 1 and not character.get_text().isspace():
                conflicts.update(owners)
            elif owners:
                chosen[owners[0]].append(character)

        blocks: list[TextBlock] = []
        completed = 0
        for number, (identifier, _) in enumerate(scopes):
            if number in conflicts:
                findings.append(
                    PdfminerTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Selected regions share source characters; neither "
                        "overlapping region was read.",
                        region_id=identifier,
                        page_index=index,
                    )
                )
                continue
            completed += 1
            # Group only this scope's characters, as pdfminer would a page.
            container = LTPage(index + 1, crop)
            for character in chosen[number]:
                container.add(character)
            container.analyze(LAParams())
            content = "\n\n".join(
                text
                for item in container
                if isinstance(item, LTTextContainer)
                and (text := item.get_text().strip("\n")).strip()
            )
            if not content:
                findings.append(
                    PdfminerTextReader._finding(
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
    def _characters(node: Any, kind: type) -> Iterator[Any]:
        """Characters in content order, including those inside form XObjects."""
        pending = [iter(node)]
        while pending:
            item = next(pending[-1], None)
            if item is None:
                pending.pop()
            elif isinstance(item, kind):
                yield item
            elif hasattr(item, "__iter__"):
                pending.append(iter(item))

    @staticmethod
    def _device_to_pdf(media: Any, rotation: int) -> Any:
        """Invert pdfminer's page transform: device boxes -> PDF space."""
        mx0, my0, mx1, my1 = (float(value) for value in media)

        def point(x: float, y: float) -> tuple[float, float]:
            if rotation == 90:
                return mx1 - y, x + my0
            if rotation == 180:
                return mx1 - x, my1 - y
            if rotation == 270:
                return y + mx0, my1 - x
            return x + mx0, y + my0

        def convert(bbox: Any) -> Box:
            (ax, ay), (bx, by) = point(bbox[0], bbox[1]), point(bbox[2], bbox[3])
            return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

        return convert

    @staticmethod
    def _backend_errors() -> tuple[type[BaseException], ...]:
        from pdfminer.pdftypes import PDFException
        from pdfminer.psparser import PSException

        return (
            PDFException,
            PSException,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            AssertionError,
            ZeroDivisionError,
            OverflowError,
            RecursionError,
            OSError,
        )

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
