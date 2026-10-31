"""Local adapters with lazy optional dependency loading."""

from .docling import DoclingLayoutService
from .paddle import PaddleLayoutService

__all__ = ["DoclingLayoutService", "PaddleLayoutService"]
