"""Independent document text readers and their common output contract."""

from .base import TextReader
from .csv import (
    UnstructuredCsvTextReader,
    CsvJsonTextReader,
    CsvTextReader,
    CsvXmlTextReader,
    PandasCsvTextReader,
)
from .docx import DocxTextReader, UnstructuredDocxTextReader
from .html import ArticleHtmlTextReader, HtmlTextReader, UnstructuredHtmlTextReader
from .json import (
    UnstructuredJsonLinesTextReader,
    UnstructuredJsonTextReader,
    JqJsonLinesTextReader,
    JqJsonTextReader,
    JsonLinesTextReader,
    JsonTextReader,
    RecordJsonTextReader,
    SubtreeJsonTextReader,
    WholeJsonTextReader,
)
from .markdown import (
    MarkdownTextReader,
    RawMarkdownTextReader,
    UnstructuredMarkdownTextReader,
)
from .models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadMethod,
    TextReadOutcome,
    TextReadResult,
)
from .ocr import (
    OcrService,
    OcrTextReader,
    OllamaOcrService,
    PaddleOcrService,
    TesseractOcrService,
)
from .pdf import (
    DoclingPdfTextReader,
    GraniteDoclingTextReader,
    MarkItDownPdfTextReader,
    PdfiumTextReader,
    PdfminerTextReader,
    PdfplumberTextReader,
    PyMuPdf4LlmTextReader,
    PyMuPdfOcrTextReader,
    PyMuPdfTextReader,
    PypdfTextReader,
)
from .plain import PlainTextReader, UnstructuredTextReader
from .pptx import PptxTextReader, UnstructuredPptxTextReader
from .xlsx import (
    UnstructuredExcelTextReader,
    PandasExcelTextReader,
    XlsxJsonTextReader,
    XlsxTextReader,
    XlsxXmlTextReader,
)

__all__ = [
    "FindingCode",
    "ArticleHtmlTextReader",
    "CsvJsonTextReader",
    "CsvXmlTextReader",
    "CsvTextReader",
    "DoclingPdfTextReader",
    "GraniteDoclingTextReader",
    "DocxTextReader",
    "HtmlTextReader",
    "JsonLinesTextReader",
    "JqJsonLinesTextReader",
    "JsonTextReader",
    "WholeJsonTextReader",
    "RecordJsonTextReader",
    "SubtreeJsonTextReader",
    "JqJsonTextReader",
    "MarkItDownPdfTextReader",
    "MarkdownTextReader",
    "OcrService",
    "OcrTextReader",
    "OllamaOcrService",
    "PaddleOcrService",
    "PandasCsvTextReader",
    "PandasExcelTextReader",
    "PdfiumTextReader",
    "PdfminerTextReader",
    "PdfplumberTextReader",
    "PlainTextReader",
    "PptxTextReader",
    "PyMuPdf4LlmTextReader",
    "PyMuPdfOcrTextReader",
    "PyMuPdfTextReader",
    "PypdfTextReader",
    "RawMarkdownTextReader",
    "TesseractOcrService",
    "TextBlock",
    "TextContentFormat",
    "TextReader",
    "TextReadFinding",
    "TextReadMethod",
    "TextReadOutcome",
    "TextReadResult",
    "UnstructuredCsvTextReader",
    "UnstructuredDocxTextReader",
    "UnstructuredExcelTextReader",
    "UnstructuredHtmlTextReader",
    "UnstructuredJsonLinesTextReader",
    "UnstructuredJsonTextReader",
    "UnstructuredMarkdownTextReader",
    "UnstructuredPptxTextReader",
    "UnstructuredTextReader",
    "XlsxJsonTextReader",
    "XlsxXmlTextReader",
    "XlsxTextReader",
]
