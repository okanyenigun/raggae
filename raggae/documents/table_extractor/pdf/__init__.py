"""Tables inferred from PDF pages."""

from .docling import DoclingTableExtractor
from .plumber import PdfplumberTableExtractor

__all__ = ["DoclingTableExtractor", "PdfplumberTableExtractor"]
