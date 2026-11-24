"""Tables found in page images with img2table: rendered PDF pages and image files."""

import warnings
from collections.abc import Sequence
from io import BytesIO
from math import isclose, isfinite
from pathlib import Path
from typing import Any
from PIL import Image
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractMethod,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import (
    failure,
    finding,
    finish,
    nothing_selected,
    place_spans,
    read_source,
    select_table_regions,
)
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion, LayoutSurface
from ...normalization.artifacts import Artifact
from ...shared.pages import ImageFrames, Pages, PdfPages
from ...shared.pdf_geometry import Box, assemble, inside, pdf_box, user_unit, visible_crop
from ...shared.sources import as_artifact
from ...text_reader.ocr.base import OcrService

type PixelBox = tuple[int, int, int, int]

# Image files read here, by Pillow's format name (MPO is a JPEG with extra pictures).
_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "MPO", "TIFF"})


class Img2TableExtractor:
    """Return the tables img2table finds in page images, with their cell text.

    PDF pages are rendered with PDFium at ``dpi`` (300 by default), as
    ``OcrTextReader`` renders them; PNG, JPEG, and TIFF files are decoded with
    Pillow, every frame included. img2table finds the grid in the pixels
    (OpenCV): tables drawn with lines by default (``method="ruled"``), and with
    ``borderless=True`` also tables without lines (``method="aligned"``). A
    grid is a hypothesis. A cell img2table merges across rows or columns gets
    that ``row_span`` or ``column_span``; a span that would cover another cell
    is shortened (``span_shortened``). No cell is a header and no table has a
    caption. ``page_index`` is the page or frame position.

    Cell text never comes from img2table. Without ``service``, a PDF cell holds
    the PDF's stored characters whose centres lie in it, put together as
    ``PdfiumTextReader`` puts them together. With ``service`` (an
    ``OcrService``), every cell's picture is recognized by it, PDF or image:
    one call per cell. An image file has no stored text, so it needs a service.
    A failing service leaves the cell empty (``ocr_failed``). A grid with no
    text is not returned (``empty_table``); a source without tables gets
    ``empty``.

    With a layout from ``NativePdfLayoutAnalyzer`` or ``VisualLayoutAnalyzer``,
    tables are searched in each requested rectangle's crop of its page or
    frame, each with that region's ID; none gives ``empty``. Without
    ``region_ids`` only table regions are used. As in ``OcrTextReader``,
    overlapping rectangles are both refused (``selection_overlap``), and a
    rectangle on a page of another size gets ``layout_mismatch``.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.NOTHING_SELECTED,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.SPAN_SHORTENED,
        }
    )

    def __init__(
        self,
        *,
        service: OcrService | None = None,
        dpi: float = 300,
        borderless: bool = False,
    ) -> None:
        if service is not None and not isinstance(service, OcrService):
            raise TypeError("service must provide recognize(image) -> str, or be None")
        if (
            isinstance(dpi, bool)
            or not isinstance(dpi, (int, float))
            or not isfinite(dpi)
            or dpi <= 0
        ):
            raise ValueError("dpi must be a positive number")
        if not isinstance(borderless, bool):
            raise TypeError("borderless must be a bool")
        self._service = service
        self._dpi = float(dpi)
        self._borderless = borderless

    @property
    def name(self) -> str:
        return "table_extractor_img2table"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TableExtractResult:
        artifact = as_artifact(source)
        selected = select_table_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name, explicit=region_ids is not None)
        data = read_source(artifact, self.name)
        if isinstance(data, TableExtractResult):
            return data
        try:
            import numpy  # noqa: F401
            from img2table.tables.image import TableImage  # noqa: F401
        except ImportError as error:
            raise ImportError(
                "Tables in page images require: pip install 'raggae[table-ocr]'"
            ) from error
        document = None
        if b"%PDF-" in data[:1024]:
            opened = self._pdf(data, artifact.password)
            if isinstance(opened, TableExtractResult):
                return opened
            document, units = opened
            pages: Pages = PdfPages(document, units, self._dpi)
        else:
            frames = self._frames(data)
            if isinstance(frames, TableExtractResult):
                return frames
            if self._service is None:
                frames.close()
                raise ValueError(
                    "Image files hold no stored text; give Img2TableExtractor an OCR "
                    "service (service=...) to read their cells."
                )
            pages, units = frames, []
        surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
        try:
            return self._read(pages, document, units, selected, surfaces)
        finally:
            pages.close()

    # Sources ---------------------------------------------------------------

    def _pdf(
        self, data: bytes, password: str | None
    ) -> tuple[Any, list[float]] | TableExtractResult:
        """The PDFium document and each page's UserUnit, or a failed result."""
        import pikepdf
        import pypdfium2 as pdfium

        try:
            document = pdfium.PdfDocument(data, password=password)
        except pdfium.PdfiumError:
            # PDFium's error code can be stale; ask pikepdf why the file did not open.
            return failure(self.name, *Img2TableExtractor._refusal(data, password))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(BytesIO(data), password=password or "") as pdf:
                    units = [
                        user_unit(page.obj.get("/UserUnit", 1)) for page in pdf.pages
                    ]
            if len(units) != len(document):
                raise ValueError("page count mismatch")
        except (pikepdf.PdfError, ValueError, TypeError, KeyError):
            document.close()
            return failure(
                self.name, TableFindingCode.DECODE_FAILED, "PDF could not be opened."
            )
        if not len(document):
            document.close()
            return failure(
                self.name, TableFindingCode.NO_PAGES, "PDF contains no usable pages."
            )
        return document, units

    @staticmethod
    def _refusal(data: bytes, password: str | None) -> tuple[TableFindingCode, str]:
        import pikepdf

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(BytesIO(data), password=password or "") as pdf:
                    pages = len(pdf.pages)
        except pikepdf.PasswordError:
            code = (
                TableFindingCode.INVALID_PASSWORD
                if password
                else TableFindingCode.PASSWORD_REQUIRED
            )
            return code, "PDF could not be opened with the supplied password."
        except (pikepdf.PdfError, ValueError, OSError):
            return TableFindingCode.DECODE_FAILED, "PDF could not be opened."
        if pages == 0:
            return TableFindingCode.NO_PAGES, "PDF contains no usable pages."
        return TableFindingCode.DECODE_FAILED, "PDF could not be opened."

    def _frames(self, data: bytes) -> ImageFrames | TableExtractResult:
        try:
            image = Image.open(BytesIO(data))
            image.verify()  # Detects truncated or non-image data without decoding.
            image = Image.open(BytesIO(data))
        except Image.DecompressionBombError:
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Image exceeds Pillow's pixel limit.",
            )
        except (OSError, ValueError, SyntaxError):
            image = None
        if image is None or image.format not in _IMAGE_FORMATS:
            if image is not None:
                image.close()
            return TableExtractResult(
                extractor=self.name,
                outcome="not_applicable",
                findings=(
                    finding(
                        TableFindingCode.NOT_APPLICABLE,
                        "Source is neither a PDF nor a PNG, JPEG, or TIFF image.",
                    ),
                ),
            )
        return ImageFrames(image)

    # Pages and regions -----------------------------------------------------

    def _read(
        self,
        pages: Pages,
        document: Any,
        units: list[float],
        selected: tuple[LayoutRegion, ...] | None,
        surfaces: dict[str, LayoutSurface],
    ) -> TableExtractResult:
        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        completed = 0
        pending: dict[int, list[LayoutRegion]] = {}
        if selected is not None:
            for region in selected:
                index = pages.locate(surfaces[region.surface_id].location)
                if index is None:
                    findings.append(
                        finding(
                            TableFindingCode.SELECTION_UNSUPPORTED,
                            "Selection is not on a PDF page or image frame of this source.",
                            region_id=region.id,
                        )
                    )
                elif index >= pages.count:
                    findings.append(
                        finding(
                            TableFindingCode.PAGE_MISSING,
                            "Requested page or frame does not exist in this source.",
                            index,
                            region.id,
                        )
                    )
                else:
                    pending.setdefault(index, []).append(region)
        indexes = sorted(pending) if selected is not None else range(pages.count)
        found_any = False
        for index in indexes:
            regions = pending.get(index)
            try:
                image, size = pages.render(index)
            except (OSError, ValueError, RuntimeError):
                for region in regions if regions is not None else [None]:
                    findings.append(
                        finding(
                            TableFindingCode.PAGE_UNREADABLE,
                            "This page or frame could not be rendered or decoded.",
                            index,
                            None if region is None else region.id,
                        )
                    )
                continue
            with image:
                characters = (
                    None
                    if document is None or self._service is not None
                    else Img2TableExtractor._characters(document, index, units[index])
                )
                scopes = (
                    [(None, (0, 0, image.width, image.height))]
                    if regions is None
                    else Img2TableExtractor._scopes(
                        index, image, size, regions, surfaces, findings
                    )
                )
                for region_id, box in scopes:
                    found = self._page_tables(
                        image, size, box, characters, index, region_id, tables, findings
                    )
                    if found is None:
                        continue
                    completed += 1
                    found_any = found_any or found
                    if not found and region_id is not None:
                        findings.append(
                            finding(
                                TableFindingCode.EMPTY,
                                "No table was found in this area.",
                                index,
                                region_id,
                            )
                        )
        if selected is None and completed and not found_any:
            findings.append(
                finding(TableFindingCode.EMPTY, "No tables were found in the source.")
            )
        return finish(self.name, tables, findings, completed, self._INFORMATIONAL)

    def _page_tables(
        self,
        image: Image.Image,
        size: tuple[float, float],
        box: PixelBox,
        characters: tuple[list[tuple[str, Box]], Any] | None,
        index: int,
        region_id: str | None,
        tables: list[ExtractedTable],
        findings: list[TableExtractFinding],
    ) -> bool | None:
        """Find the tables in ``box`` of the page image: whether any was found, or
        None when detection failed."""
        import numpy
        from img2table.tables.image import TableImage

        left, top = box[0], box[1]
        with image.crop(box) as crop:
            try:
                found = TableImage(img=numpy.array(crop.convert("RGB"))).extract_tables(
                    implicit_rows=False,
                    implicit_columns=False,
                    borderless_tables=self._borderless,
                )
            # img2table and OpenCV raise assorted errors on unusual pixels.
            except Exception:  # noqa: BLE001
                findings.append(
                    finding(
                        TableFindingCode.PAGE_UNREADABLE,
                        "Tables on this page or area could not be detected.",
                        index,
                        region_id,
                    )
                )
                return None
        shortened = failed = False
        for detected in found:
            method: TableExtractMethod = "aligned" if detected._borderless else "ruled"
            cells, rows, columns, cut, missed = self._grid(
                detected.extracted_table, (left, top), image, size, characters
            )
            shortened, failed = shortened or cut, failed or missed
            if not any(cell.text.strip() for cell in cells):
                findings.append(
                    finding(
                        TableFindingCode.EMPTY_TABLE,
                        "A table with no text in any cell was not returned.",
                        index,
                        region_id,
                    )
                )
                continue
            tables.append(
                ExtractedTable(
                    cells=tuple(cells),
                    row_count=rows,
                    column_count=columns,
                    page_index=index,
                    region_id=region_id,
                    method=method,
                )
            )
        if shortened:
            findings.append(
                finding(
                    TableFindingCode.SPAN_SHORTENED,
                    "A cell's span ran into another cell and was shortened.",
                    index,
                    region_id,
                )
            )
        if failed:
            findings.append(
                finding(
                    TableFindingCode.OCR_FAILED,
                    "The OCR service failed on some cells; they were left empty.",
                    index,
                    region_id,
                )
            )
        return bool(found)

    def _grid(
        self,
        table: Any,
        offset: tuple[int, int],
        image: Image.Image,
        size: tuple[float, float],
        characters: tuple[list[tuple[str, Box]], Any] | None,
    ) -> tuple[list[TableCell], int, int, bool, bool]:
        """Cells from img2table's rows: positions sharing a cell box form one cell.

        Returns the cells, the row and column counts, whether a span was
        shortened, and whether the OCR service failed on a cell.
        """
        boxes: dict[PixelBox, list[tuple[int, int]]] = {}
        rows = list(table.content.values())
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                bbox = cell.bbox
                key = (
                    int(bbox.x1) + offset[0],
                    int(bbox.y1) + offset[1],
                    int(bbox.x2) + offset[0],
                    int(bbox.y2) + offset[1],
                )
                boxes.setdefault(key, []).append((r, c))
        anchors = {min(positions): key for key, positions in boxes.items()}
        wanted = {
            anchor: (
                max(r for r, _ in boxes[key]) - anchor[0] + 1,
                max(c for _, c in boxes[key]) - anchor[1] + 1,
            )
            for anchor, key in anchors.items()
        }
        spans, shortened = place_spans(wanted)
        ordered = sorted(spans)
        texts, failed = self._texts(
            [anchors[a] for a in ordered], image, size, characters
        )
        cells = [
            TableCell(
                row=r,
                column=c,
                row_span=spans[(r, c)][0],
                column_span=spans[(r, c)][1],
                text=text,
            )
            for (r, c), text in zip(ordered, texts)
        ]
        columns = max((len(row) for row in rows), default=0)
        return cells, len(rows), columns, shortened, failed

    def _texts(
        self,
        boxes: list[PixelBox],
        image: Image.Image,
        size: tuple[float, float],
        characters: tuple[list[tuple[str, Box]], Any] | None,
    ) -> tuple[list[str], bool]:
        """Each cell's text: recognized by the service, or the PDF's stored characters."""
        if self._service is not None:
            texts: list[str] = []
            failed = False
            for box in boxes:
                inner = Img2TableExtractor._inset(box)
                with image.crop(inner) as picture:
                    try:
                        texts.append(self._service.recognize(picture).strip())
                    # The service is injected; any failure is reported per area.
                    except Exception:  # noqa: BLE001
                        texts.append("")
                        failed = True
            return texts, failed
        if characters is None:
            return ["" for _ in boxes], False
        chars, (crop, rotation, unit) = characters
        scale_x, scale_y = size[0] / image.width, size[1] / image.height
        areas = [
            pdf_box(
                (
                    box[0] * scale_x,
                    box[1] * scale_y,
                    box[2] * scale_x,
                    box[3] * scale_y,
                ),
                crop,
                rotation,
                unit,
            )
            for box in boxes
        ]
        owners: list[list[int]] = []
        for character, char_box in chars:
            owner = next(
                (number for number, area in enumerate(areas) if inside(area, char_box)),
                None,
            )
            owners.append([] if owner is None or character.isspace() else [owner])
        return [assemble(chars, owners, number) for number in range(len(boxes))], False

    @staticmethod
    def _characters(
        document: Any, index: int, unit: float
    ) -> tuple[list[tuple[str, Box]], tuple[Box, int, float]] | None:
        """The page's stored characters with their PDF-space boxes, and its geometry."""
        import pypdfium2 as pdfium

        page = document[index]
        try:
            rotation = page.get_rotation()
            crop = visible_crop(page.get_mediabox(), page.get_cropbox(), rotation)
            text = page.get_textpage()
            try:
                chars = [
                    (text.get_text_range(position, 1), text.get_charbox(position))
                    for position in range(text.count_chars())
                ]
            finally:
                text.close()
        except (pdfium.PdfiumError, ValueError):
            return None
        finally:
            page.close()
        return chars, (crop, rotation, unit)

    @staticmethod
    def _scopes(
        index: int,
        image: Image.Image,
        size: tuple[float, float],
        regions: list[LayoutRegion],
        surfaces: dict[str, LayoutSurface],
        findings: list[TableExtractFinding],
    ) -> list[tuple[str | None, PixelBox]]:
        """Pixel crops for this page's regions; overlapping regions are refused."""
        width, height = size
        scale_x, scale_y = image.width / width, image.height / height
        boxes: list[tuple[str, PixelBox]] = []
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
                    finding(
                        TableFindingCode.LAYOUT_MISMATCH,
                        "Layout dimensions do not match the source page or frame.",
                        index,
                        region.id,
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
            boxes.append((region.id, box))
        overlapping = {
            identifier
            for position, (identifier, box) in enumerate(boxes)
            for other_id, other in boxes[position + 1 :]
            if min(box[2], other[2]) > max(box[0], other[0])
            and min(box[3], other[3]) > max(box[1], other[1])
            for identifier in (identifier, other_id)
        }
        for identifier, _ in boxes:
            if identifier in overlapping:
                findings.append(
                    finding(
                        TableFindingCode.SELECTION_OVERLAP,
                        "Selected regions overlap; neither overlapping region was read.",
                        index,
                        identifier,
                    )
                )
        return [(i, box) for i, box in boxes if i not in overlapping]

    @staticmethod
    def _inset(box: PixelBox) -> PixelBox:
        """The cell without the ruling lines along its edges, when the cell allows it."""
        left, top, right, bottom = box
        margin = 4
        if right - left > 4 * margin and bottom - top > 4 * margin:
            return left + margin, top + margin, right - margin, bottom - margin
        return box
