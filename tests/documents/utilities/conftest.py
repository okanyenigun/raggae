"""Reuse the small PDF fixture without duplicating PDF authoring code."""

from ..layout_analysis.conftest import make_pdf

__all__ = ["make_pdf"]
