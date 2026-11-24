"""Assemble the lines of an HTML block the way a browser collapses whitespace.

Moved to ``raggae.documents.shared.html_text``, shared with the HTML table extractor;
imported back here so this import path keeps working.
"""

from ...shared.html_text import TextLines

__all__ = ["TextLines"]
