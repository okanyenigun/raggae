"""Whole-document plain-text decoding."""

from .whole import PlainTextReader
from .unstructured import UnstructuredTextReader

__all__ = ["UnstructuredTextReader", "PlainTextReader"]
