"""Independent document table extractors and their common output contract."""

from .base import TableExtractor
from .csv import CsvTableExtractor
from .models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractMethod,
    TableExtractOutcome,
    TableExtractResult,
    TableFindingCode,
)
from .docx import DocxTableExtractor
from .html import HtmlTableExtractor
from .image import Img2TableExtractor
from .markdown import MarkdownTableExtractor
from .pdf import DoclingTableExtractor, PdfplumberTableExtractor
from .pptx import PptxTableExtractor
from .xlsx import XlsxTableExtractor

__all__ = [
    "CsvTableExtractor",
    "DoclingTableExtractor",
    "DocxTableExtractor",
    "ExtractedTable",
    "HtmlTableExtractor",
    "Img2TableExtractor",
    "MarkdownTableExtractor",
    "PdfplumberTableExtractor",
    "PptxTableExtractor",
    "TableCell",
    "TableExtractFinding",
    "TableExtractMethod",
    "TableExtractOutcome",
    "TableExtractResult",
    "TableExtractor",
    "TableFindingCode",
    "XlsxTableExtractor",
]
