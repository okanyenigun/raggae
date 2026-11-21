"""OCR text reading with an injected recognition service."""

from .base import OcrService
from .reader import OcrTextReader
from .services import OllamaOcrService, PaddleOcrService, TesseractOcrService

__all__ = [
    "OcrService",
    "OcrTextReader",
    "OllamaOcrService",
    "PaddleOcrService",
    "TesseractOcrService",
]
