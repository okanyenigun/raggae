"""PowerPoint readers using the common text result model."""

from .slides import PptxTextReader
from .unstructured import UnstructuredPptxTextReader

__all__ = ["UnstructuredPptxTextReader", "PptxTextReader"]
