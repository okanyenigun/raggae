"""The workbook as the readers see it: rows of cells and the groups of cells to read."""

from collections.abc import Iterator
from typing import Any
from .geometry import Box, Owned, cells_in, components, contains, point, range_box
from .values import MISSING, Cell


class Book:
    """The formula workbook, paired with the saved-result workbook when needed."""

    def __init__(self, formulas: Any, cached: Any | None) -> None:
        self.formulas = formulas
        self.cached = cached

    def rows(
        self, name: str, box: Box, owned: Owned
    ) -> Iterator[tuple[int, list[Cell]]]:
        """Each row of ``box``; cells failing ``owned`` read as empty.

        A merged cell is read once: columns it spans beyond its first are skipped.
        """
        left, top, right, bottom = box
        sheet = self.formulas[name]
        source = sheet._cells
        values = None if self.cached is None else self.cached[name]._cells
        spanned = {
            (row, column)
            for merged in sheet.merged_cells.ranges
            for row in range(merged.min_row, merged.max_row + 1)
            for column in range(merged.min_col + 1, merged.max_col + 1)
        }
        for row in range(top, bottom + 1):
            cells: list[Cell] = []
            for column in range(left, right + 1):
                if (row, column) in spanned:
                    continue
                cell = source.get((row, column))
                if cell is None or cell.value is None or not owned((row, column)):
                    cells.append(Cell(column, None))
                elif cell.data_type == "f" and values is not None:
                    saved = values.get((row, column))
                    value = None if saved is None else saved.value
                    cells.append(Cell(column, MISSING if value is None else value))
                else:
                    cells.append(Cell(column, cell.value))
            yield row, cells


def is_worksheet(sheet: Any) -> bool:
    from openpyxl.worksheet.worksheet import Worksheet

    return isinstance(sheet, Worksheet)


def sheet_blocks(sheet: Any) -> list[tuple[Box, frozenset[tuple[int, int]]]]:
    """Declared tables, merged cells with a value, then connected filled cells."""
    blocks: list[tuple[Box, frozenset[tuple[int, int]]]] = []
    covered: list[Box] = []
    for table in sheet.tables.values():
        box = range_box(table.ref)
        covered.append(box)
        blocks.append((box, cells_in(box)))
    for merged in sorted(
        sheet.merged_cells.ranges, key=lambda item: (item.min_row, item.min_col)
    ):
        top_left = sheet._cells.get((merged.min_row, merged.min_col))
        if top_left is None or top_left.value is None:
            continue
        box = merged.bounds
        if any(contains(outer, box) for outer in covered):
            continue
        covered.append(box)
        blocks.append((box, frozenset({(merged.min_row, merged.min_col)})))
    occupied = {
        position
        for position, cell in sheet._cells.items()
        if cell.value is not None
        and not any(contains(outer, point(position)) for outer in covered)
    }
    for cells in components(occupied):
        rows = [row for row, _ in cells]
        columns = [column for _, column in cells]
        blocks.append(((min(columns), min(rows), max(columns), max(rows)), cells))
    return blocks
