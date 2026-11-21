"""OCR PDF pages with the Tesseract engine built into PyMuPDF.

PyMuPDF is licensed AGPL-3.0 or under an Artifex commercial licence; it is
installed only through the opt-in ``raggae[text-pymupdf]`` extra.
"""

import re
from math import isfinite
from pathlib import Path
from typing import Any
from .pymupdf import PyMuPdfTextReader
from ..models import TextReadMethod
from ...layout_analysis.models import LayoutRegion


class PyMuPdfOcrTextReader(PyMuPdfTextReader):
    """OCR whole PDF pages with PyMuPDF's built-in Tesseract, by page or region.

    Each page is rendered as displayed (crop and rotation applied, no
    annotations) at ``dpi`` (default 300) and recognized as a whole; stored
    text is ignored, so every block has ``method="ocr"``. Region reads OCR the
    page once and keep recognized characters whose centre lies in each region,
    exactly like ``PyMuPdfTextReader`` (regions sharing characters are both
    rejected); image regions are accepted. ``language`` (for example ``"eng"``
    or ``"deu+eng"``) and ``tessdata``—the folder holding each language's
    ``.traineddata`` file—are required and checked at construction; nothing is
    searched for or downloaded. PDF only. Requires the opt-in
    ``raggae[text-pymupdf]`` extra (AGPL-3.0/commercial).
    """

    _LANGUAGE = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*")
    _method: TextReadMethod = "ocr"
    _empty_message = "No text was recognized in this page or region."
    _unreadable_message = "This PDF page could not be rendered or recognized."

    def __init__(
        self, *, language: str, tessdata: Path | str, dpi: float = 300
    ) -> None:
        if not isinstance(language, str):
            raise TypeError("language must be a string such as 'eng' or 'deu+eng'")
        if not self._LANGUAGE.fullmatch(language):
            raise ValueError("language must look like 'eng' or 'deu+eng'")
        if not isinstance(tessdata, (Path, str)):
            raise TypeError("tessdata must be the path of a Tesseract tessdata folder")
        folder = Path(tessdata).expanduser()
        if not folder.is_dir():
            raise ValueError("tessdata must be an existing Tesseract tessdata folder")
        missing = [
            code
            for code in language.split("+")
            if not (folder / f"{code}.traineddata").is_file()
        ]
        if missing:
            raise ValueError(
                f"Tesseract language data {', '.join(repr(code) for code in missing)} "
                f"is not in {folder}; install it locally (it is never downloaded)."
            )
        if (
            isinstance(dpi, bool)
            or not isinstance(dpi, (int, float))
            or not isfinite(dpi)
            or dpi <= 0
        ):
            raise ValueError("dpi must be a positive number")
        self._language = language
        self._tessdata = str(folder.resolve())
        self._dpi = float(dpi)

    @property
    def name(self) -> str:
        return "text_reader_pymupdf_ocr"

    @property
    def language(self) -> str:
        return self._language

    def _accepts(self, region: LayoutRegion) -> bool:
        return True

    def _raw_blocks(self, page: Any, unit: float) -> list[dict[str, Any]]:
        import pymupdf
        from pymupdf.mupdf import FzErrorBase

        try:
            zoom = self._dpi / 72
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=False)
            # Tells Tesseract the resolution; the OCR page is mapped back by size.
            resolution = max(1, round(self._dpi))
            pixmap.set_dpi(resolution, resolution)
            recognized = pymupdf.open(
                stream=pixmap.pdfocr_tobytes(
                    compress=False, language=self._language, tessdata=self._tessdata
                ),
                filetype="pdf",
            )
        except FzErrorBase as error:
            raise RuntimeError("page could not be rendered or recognized") from error
        try:
            ocr_page = recognized[0]
            # The OCR page has the displayed page's size; map it back to the
            # unrotated space the parent class expects. PyMuPDF multiplies page
            # sizes and text boxes by /UserUnit but not derotation_matrix, so
            # derotate in unscaled points and scale by the unit afterwards.
            scale = page.rect.width / ocr_page.rect.width / unit
            matrix = (
                pymupdf.Matrix(scale, scale)
                * page.derotation_matrix
                * pymupdf.Matrix(unit, unit)
            )
            content: Any = ocr_page.get_text("rawdict")
            blocks: list[dict[str, Any]] = content["blocks"]
        except FzErrorBase as error:
            raise RuntimeError("page could not be recognized") from error
        finally:
            recognized.close()
        # Characters are mapped here rather than with a text-page matrix, which
        # would still clip them to the OCR page's displayed (rotated) box.
        for block in blocks:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    for char in span["chars"]:
                        char["bbox"] = tuple(pymupdf.Rect(char["bbox"]) * matrix)
        return blocks
