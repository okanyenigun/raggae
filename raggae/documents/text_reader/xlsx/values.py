"""Cell values: the missing-result marker, number and date formats, and cell text."""

import datetime
from dataclasses import dataclass
from typing import Any, Literal

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


def cell_text(value: Any) -> str:
    """Stored values, not Excel's display formats; one line per cell."""
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
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
