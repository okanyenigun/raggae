"""Backend-agnostic visual layout and interchangeable local services."""

from .base import LayoutDetection, LayoutDetectionService
from .services import DoclingLayoutService, PaddleLayoutService
from .worker import VisualLayoutAnalyzer

__all__ = [
    "LayoutDetection", "LayoutDetectionService", "DoclingLayoutService",
    "PaddleLayoutService", "VisualLayoutAnalyzer",
]
