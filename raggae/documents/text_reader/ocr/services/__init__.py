"""OCR services for OcrTextReader."""

from .ollama import OllamaOcrService
from .paddle import PaddleOcrService
from .tesseract import TesseractOcrService

__all__ = ["OllamaOcrService", "PaddleOcrService", "TesseractOcrService"]
