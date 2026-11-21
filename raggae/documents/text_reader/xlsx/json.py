"""Represent each worksheet row as JSON content with its sheet and row number."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from .book import Book
from .flow import read_workbook
from .geometry import Box, Owned, contains, range_box
from .values import MISSING, CellValues, number, require_cell_values, temporal
from ..models import TextBlock, TextContentFormat, TextReadFinding, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ...normalization.artifacts import Artifact


class XlsxJsonTextReader:
    """Return one JSON-content block per filled worksheet row.

    Each block is ``{"sheet": ..., "row": ..., "cells": {...}}`` with the
    Excel row number. Inside a declared Excel table, cells are keyed by the
    table's column names, ``"table"`` names the table, and the header row is
    not emitted; elsewhere cells are keyed by column letter and no header is
    guessed. Values keep their types: numbers, booleans, strings, and ISO 8601
    strings for dates and times. Empty cells are left out; a formula without a
    saved result is ``null`` and reported. Cell grouping, ``page_index``,
    ``cell_values``, region selection, and findings match ``XlsxTextReader``.
    """

    def __init__(self, *, cell_values: CellValues = "cached") -> None:
        self._cell_values: CellValues = require_cell_values(cell_values)

    @property
    def name(self) -> str:
        return "text_reader_xlsx_json"

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
            missing_effect="written as null",
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
        """One block per row that has at least one value or unsaved result."""
        from openpyxl.utils.cell import get_column_letter

        tables = [
            (range_box(table.ref), table)
            for table in book.formulas[name].tables.values()
        ]
        blocks: list[TextBlock] = []
        missing = 0
        for row, cells in book.rows(name, box, owned):
            filled = [cell for cell in cells if cell.value is not None]
            if not filled:
                continue
            table = next(
                (
                    (table_box, table)
                    for table_box, table in tables
                    if all(
                        contains(table_box, (cell.column, row, cell.column, row))
                        for cell in filled
                    )
                ),
                None,
            )
            keys = {cell.column: get_column_letter(cell.column) for cell in filled}
            content: dict[str, Any] = {"sheet": name, "row": row}
            if table is not None:
                (left, top, _, _), declared = table
                if row < top + (declared.headerRowCount or 0):
                    continue  # The header row names the keys; it is not data.
                names = [column.name for column in declared.tableColumns]
                if all(0 <= cell.column - left < len(names) for cell in filled):
                    keys = {cell.column: names[cell.column - left] for cell in filled}
                    content["table"] = declared.displayName
            values: dict[str, Any] = {}
            for cell in filled:
                if cell.value is MISSING:
                    missing += 1
                values[keys[cell.column]] = XlsxJsonTextReader._value(cell.value)
            content["cells"] = values
            blocks.append(
                TextBlock(
                    text=json.dumps(content, separators=(",", ":"), allow_nan=False),
                    page_index=index,
                    content_format=TextContentFormat.JSON,
                    region_id=None if region is None else region.id,
                )
            )
        return blocks, missing, []

    @staticmethod
    def _value(value: Any) -> Any:
        """Typed JSON values; dates and times become ISO 8601 strings."""
        if value is MISSING:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, float):
            return number(value)
        if isinstance(value, (int, str)):
            return value
        if (iso := temporal(value)) is not None:
            return iso
        return getattr(value, "text", None) or str(value)  # e.g. ArrayFormula
