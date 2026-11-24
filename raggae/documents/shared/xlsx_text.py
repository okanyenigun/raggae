"""Worksheet cells as the Excel text readers read them; shared by the Excel
text readers and the Excel table extractor.

Cell values: stored values, never Excel's display formats; formulas are never
evaluated, and a formula without a saved result is ``MISSING``. Cell boxes:
(min column, min row, max column, max row), as openpyxl counts, from 1.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from ..layout_analysis.models import LayoutRegion

type CellValues = Literal["cached", "formulas"]

MISSING = object()  # A formula cell whose saved result is absent.


@dataclass(frozen=True, slots=True)
class Cell:
    column: int
    value: Any  # None for an empty cell, MISSING for an unsaved formula result.


def require_cell_values(cell_values: CellValues) -> CellValues:
    if not isinstance(cell_values, str):
        raise TypeError("cell_values must be a string")
    if cell_values not in ("cached", "formulas"):
        raise ValueError("cell_values must be 'cached' or 'formulas'")
    return cell_values


def number(value: float) -> int | float:
    """Whole doubles become integers only while exact; beyond 2**53 keep the double."""
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


def temporal(value: Any) -> str | None:
    """ISO 8601 for dates, times, and durations; None for other values."""
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time() and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return _duration(value)
    return None


def _duration(value: datetime.timedelta) -> str:
    """ISO 8601 duration, e.g. P1DT2H5M."""
    sign = "-" if value < datetime.timedelta() else ""
    value = abs(value)
    hours, remainder = divmod(value.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    seconds_text = (
        f"{seconds}.{value.microseconds:06d}".rstrip("0")
        if value.microseconds
        else str(seconds)
    )
    time = "".join(
        f"{amount}{unit}" for amount, unit in ((hours, "H"), (minutes, "M")) if amount
    ) + (f"{seconds_text}S" if seconds or value.microseconds else "")
    days = f"{value.days}D" if value.days else ""
    if not days and not time:
        return "PT0S"
    return f"{sign}P{days}" + (f"T{time}" if time else "")


def value_text(value: Any) -> str:
    """Stored values, not Excel's display formats; line breaks kept."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif isinstance(value, float):
        stored = number(value)
        text = repr(stored) if isinstance(stored, float) else str(stored)
    elif isinstance(value, int):
        text = str(value)
    elif (iso := temporal(value)) is not None:
        text = iso
    elif isinstance(value, str):
        text = value
    else:
        text = getattr(value, "text", None) or str(value)  # e.g. ArrayFormula
    return text


def cell_text(value: Any) -> str:
    """Stored values, not Excel's display formats; one line per cell."""
    return value_text(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def stored_value(cell: Any, cached: dict[tuple[int, int], Any] | None) -> Any:
    """A cell's value as the readers read it.

    None for an empty cell. A formula cell gives its saved result when ``cached``
    (the saved-result workbook's cells) is given, or ``MISSING`` when there is
    none; otherwise the stored value, formula text included.
    """
    if cell is None or cell.value is None:
        return None
    if cell.data_type == "f" and cached is not None:
        saved = cached.get((cell.row, cell.column))
        value = None if saved is None else saved.value
        return MISSING if value is None else value
    return cell.value


# Cell boxes -------------------------------------------------------------------

type Box = tuple[int, int, int, int]  # (min_col, min_row, max_col, max_row)
type Owned = Callable[[tuple[int, int]], bool]


def range_box(reference: str) -> Box:
    """Bounds of a bounded A1 range; whole-row or whole-column ranges are invalid."""
    from openpyxl.utils.cell import range_boundaries

    left, top, right, bottom = range_boundaries(reference)
    if left is None or top is None or right is None or bottom is None:
        raise ValueError("range must have bounded rows and columns")
    return left, top, right, bottom


def cells_in(box: Box) -> frozenset[tuple[int, int]]:
    left, top, right, bottom = box
    return frozenset(
        (row, column)
        for row in range(top, bottom + 1)
        for column in range(left, right + 1)
    )


def components(cells: set[tuple[int, int]]) -> list[frozenset[tuple[int, int]]]:
    """Four-neighbour groups, in row-major order of their first cell."""
    remaining = set(cells)
    groups: list[frozenset[tuple[int, int]]] = []
    for first in sorted(cells):
        if first not in remaining:
            continue
        remaining.remove(first)
        pending, group = [first], {first}
        while pending:
            row, column = pending.pop()
            for neighbour in (
                (row - 1, column),
                (row + 1, column),
                (row, column - 1),
                (row, column + 1),
            ):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    group.add(neighbour)
                    pending.append(neighbour)
        groups.append(frozenset(group))
    return groups


def point(cell: tuple[int, int]) -> Box:
    """A (row, column) cell as a one-cell box."""
    row, column = cell
    return column, row, column, row


def contains(outer: Box, inner: Box) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _intersects(first: Box, second: Box) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def conflicts(resolved: list[tuple[LayoutRegion, str, Box]]) -> set[str]:
    """IDs of ranges that share cells without nesting, or that are identical."""
    rejected: set[str] = set()
    for position, (region, name, box) in enumerate(resolved):
        for other_region, other_name, other in resolved[position + 1 :]:
            if name != other_name or not _intersects(box, other):
                continue
            nested = box != other and (contains(box, other) or contains(other, box))
            if not nested:
                rejected.update({region.id, other_region.id})
    return rejected


def is_worksheet(sheet: Any) -> bool:
    """Whether the sheet holds cells (not a chart sheet)."""
    from openpyxl.worksheet.worksheet import Worksheet

    return isinstance(sheet, Worksheet)
