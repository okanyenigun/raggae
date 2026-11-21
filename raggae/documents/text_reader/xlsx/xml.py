"""Represent each worksheet row as an XML element with its sheet and row number."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from .book import Book
from .flow import finding, read_workbook
from .geometry import Box, Owned, contains, range_box
from .values import MISSING, CellValues, number, require_cell_values, temporal
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ..utils.xml_text import XmlText
from ...layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ...normalization.artifacts import Artifact


class XlsxXmlTextReader:
    """Return one XML-content block per filled worksheet row.

    Each block is ``<row sheet="..." row="..." table="...">`` with one child
    element per filled cell: ``<row sheet="Orders" row="2"><A>Labels</A></row>``.
    Inside a declared Excel table, ``table`` names the table, cells are named
    by the table's column names, and the header row is not emitted; a table
    column name that is not a valid XML name makes that table's rows use
    column letters instead, with a finding. Elsewhere cells are named by column
    letter and no header is guessed. Values are text as stored: numbers, ``true`` or
    ``false``, and ISO 8601 for dates and times; markup characters are escaped.
    Empty cells are left out; a formula without a saved result is an empty
    element and reported. A row holding a character XML cannot represent is
    left out and reported. Cell grouping, ``page_index``, ``cell_values``,
    region selection, and other findings match ``XlsxTextReader``.
    """

    def __init__(self, *, cell_values: CellValues = "cached") -> None:
        self._cell_values: CellValues = require_cell_values(cell_values)
        self._xml = XmlText()

    @property
    def name(self) -> str:
        return "text_reader_xlsx_xml"

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
            missing_effect="written as an empty element",
        )

    def _render(
        self,
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
        notes: list[TextReadFinding] = []
        missing = 0
        left_out = 0
        letter_tables: set[str] = set()
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
            attributes = self._xml.attribute("sheet", name) + self._xml.attribute(
                "row", str(row)
            )
            if table is not None:
                (left, top, _, _), declared = table
                if row < top + (declared.headerRowCount or 0):
                    continue  # The header row names the tags; it is not data.
                attributes += self._xml.attribute("table", declared.displayName)
                names = [column.name for column in declared.tableColumns]
                declared_keys = {
                    cell.column: names[cell.column - left] for cell in filled
                }
                if all(self._xml.is_name(key) for key in declared_keys.values()):
                    keys = declared_keys
                else:
                    letter_tables.add(declared.displayName)
            texts = {
                cell.column: None if cell.value is MISSING else self._text(cell.value)
                for cell in filled
            }
            if any(
                text is not None and self._xml.has_illegal(text)
                for text in texts.values()
            ):
                left_out += 1
                continue
            missing += sum(1 for text in texts.values() if text is None)
            children = "".join(
                self._xml.element(keys[column], text or "")
                for column, text in texts.items()
            )
            blocks.append(
                TextBlock(
                    text=f"<row{attributes}>{children}</row>",
                    page_index=index,
                    content_format=TextContentFormat.XML,
                    region_id=None if region is None else region.id,
                )
            )
        if letter_tables:
            notes.append(
                finding(
                    FindingCode.HEADER_INVALID,
                    "A table here has a column name that is not a valid XML tag "
                    "name; its rows are named by column letter.",
                    region=region,
                    page_index=index,
                )
            )
        if left_out:
            rows = "row holds" if left_out == 1 else "rows hold"
            notes.append(
                finding(
                    FindingCode.INVALID_CHARACTER,
                    f"{left_out} {rows} a character XML cannot represent; "
                    "left out of the result.",
                    region=region,
                    page_index=index,
                )
            )
        return blocks, missing, notes

    @staticmethod
    def _text(value: Any) -> str:
        """The value as text, not Excel's display format; dates and times in ISO 8601."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            stored = number(value)
            return repr(stored) if isinstance(stored, float) else str(stored)
        if isinstance(value, (int, str)):
            return str(value)
        if (iso := temporal(value)) is not None:
            return iso
        return getattr(value, "text", None) or str(value)  # e.g. ArrayFormula
