"""The pages or frames of a source, drawn on demand as pictures for recognition.

``ImageFrames`` moved to ``raggae.documents.shared.pages``, shared with the table
extractors; imported back here so this import path keeps working.
"""

from ...shared.pages import ImageFrames, Pages, PdfPages

__all__ = ["ImageFrames", "Pages", "PdfPages"]
