"""Cell boxes (min column, min row, max column, max row) and how they relate.

Moved to ``raggae.documents.shared.xlsx_text``, shared with the Excel table extractor;
imported back here so this import path keeps working.
"""

from ...shared.xlsx_text import (
    Box,
    Owned,
    cells_in,
    components,
    conflicts,
    contains,
    point,
    range_box,
)

__all__ = [
    "Box",
    "Owned",
    "cells_in",
    "components",
    "conflicts",
    "contains",
    "point",
    "range_box",
]
