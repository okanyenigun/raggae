"""The small boundary between page/frame handling and a text recognizer."""

from typing import Protocol, runtime_checkable
from PIL import Image


@runtime_checkable
class OcrService(Protocol):
    def recognize(self, image: Image.Image) -> str:
        """Recognize the text in one RGB image; do not close or retain it.

        Return the recognized text, or an empty string when nothing was
        recognized. Never invent boxes or confidences. Failures must raise,
        never masquerade as an empty result.
        """
        ...
