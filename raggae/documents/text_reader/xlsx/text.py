"""Read worksheet cell blocks as rows, without evaluating formulas."""

from collections.abc import Sequence
from pathlib import Path
from .book import Book
from .flow import read_workbook
from .geometry import Box, Owned
from .values import MISSING, CellValues, cell_text, require_cell_values
from ..models import TextBlock, TextReadFinding, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ...normalization.artifacts import Artifact


class XlsxTextReader:
    """Return plain-text blocks of worksheet cells, with ``page_index`` = sheet position.

    Without a layout, each declared table, merged cell with a value, or
    connected area of filled cells becomes one block, matching
    ``XlsxLayoutAnalyzer``. With a layout, each selected cell range becomes one
    block; a range inside another selected range owns its cells. Rows are
    lines with cells joined by `` | ``; empty cells keep their column. By
    default formula cells give their saved result (``cell_values="cached"``);
    ``cell_values="formulas"`` returns formula text. Formulas are never
    evaluated, and a missing saved result is reported, not invented. Numbers
    are written as stored, dates in ISO 8601. Hidden sheets, rows, and columns
    are read and reported. Images, charts, and comments are not read.
    """

    def __init__(self, *, cell_values: CellValues = "cached") -> None:
        self._cell_values: CellValues = require_cell_values(cell_values)

    @property
    def name(self) -> str:
        return "text_reader_xlsx"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        return read_workbook(
            source,
            layout=layout,
            region_ids=region_ids,
            reader=self.name,
            cell_values=self._cell_values,
            render=self._render,
            missing_effect="left empty",
        )

    @staticmethod
    def _render(
        book: Book,
        name: str,
        index: int,
        box: Box,
        owned: Owned,
        region: LayoutRegion | None,
    ) -> tuple[list[TextBlock], int, list[TextReadFinding]]:
        """One block per cell group: rows as lines, cells joined by `` | ``."""
        lines: list[str] = []
        missing = 0
        for _, cells in book.rows(name, box, owned):
            texts: list[str] = []
            for cell in cells:
                if cell.value is MISSING:
                    missing += 1
                    texts.append("")
                else:
                    texts.append(cell_text(cell.value))
            if any(texts):
                lines.append(" | ".join(texts))
        if not lines:
            return [], missing, []
        return (
            [
                TextBlock(
                    text="\n".join(lines),
                    page_index=index,
                    region_id=None if region is None else region.id,
                )
            ],
            missing,
            [],
        )
