"""Native PDF text reading, independently or against a supplied layout map."""

from .docling import DoclingPdfTextReader
from .granite_docling import GraniteDoclingTextReader
from .markitdown import MarkItDownPdfTextReader
from .pdfium import PdfiumTextReader
from .pdfminer import PdfminerTextReader
from .plumber import PdfplumberTextReader
from .pymupdf import PyMuPdfTextReader
from .pymupdf4llm import PyMuPdf4LlmTextReader
from .pymupdf_ocr import PyMuPdfOcrTextReader
from .pypdf import PypdfTextReader

__all__ = [
    "DoclingPdfTextReader",
    "GraniteDoclingTextReader",
    "MarkItDownPdfTextReader",
    "PdfiumTextReader",
    "PdfminerTextReader",
    "PdfplumberTextReader",
    "PyMuPdf4LlmTextReader",
    "PyMuPdfOcrTextReader",
    "PyMuPdfTextReader",
    "PypdfTextReader",
]
