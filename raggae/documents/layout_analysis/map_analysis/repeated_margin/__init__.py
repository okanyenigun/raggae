"""Standalone repeated-margin detection and its small result schema."""

from .base import RepeatedMarginDetector
from .models import RepeatedMarginGroup, RepeatedMarginResult
from .pdf import PdfRepeatedMarginDetector

__all__ = ["PdfRepeatedMarginDetector", "RepeatedMarginDetector", "RepeatedMarginGroup", "RepeatedMarginResult"]
