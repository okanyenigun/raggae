"""Word readers using the common text result model."""

from .body import DocxTextReader
from .unstructured import UnstructuredDocxTextReader

__all__ = ["UnstructuredDocxTextReader", "DocxTextReader"]
