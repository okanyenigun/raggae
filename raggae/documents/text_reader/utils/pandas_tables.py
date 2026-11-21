"""Option checks and row rendering shared by the pandas readers."""

from collections.abc import Mapping
from typing import Any

# Options pandas users rely on that cannot change value types, fetch data, or
# stream chunks. Everything else is refused at construction.
CSV_OPTIONS = frozenset(
    {
        "sep",
        "delimiter",
        "header",
        "names",
        "usecols",
        "skiprows",
        "skipfooter",
        "nrows",
        "comment",
        "skip_blank_lines",
        "quotechar",
        "quoting",
        "doublequote",
        "escapechar",
        "lineterminator",
        "skipinitialspace",
        "on_bad_lines",
        "engine",
    }
)
EXCEL_OPTIONS = frozenset(
    {"sheet_name", "header", "names", "usecols", "skiprows", "skipfooter", "nrows"}
)


def allowed_options(
    options: Mapping[str, Any] | None, allowed: frozenset[str]
) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, Mapping):
        raise TypeError("options must be a mapping of pandas option names to values")
    if any(not isinstance(key, str) for key in options):
        raise TypeError("option names must be strings")
    refused = sorted(set(options) - allowed)
    if refused:
        raise ValueError(
            f"option {refused[0]!r} is not allowed; allowed options: "
            + ", ".join(sorted(allowed))
        )
    if "on_bad_lines" in options and options["on_bad_lines"] not in (
        "error",
        "warn",
        "skip",
    ):
        raise ValueError("on_bad_lines must be 'error', 'warn', or 'skip'")
    if "engine" in options and options["engine"] not in ("c", "python"):
        raise ValueError("engine must be 'c' or 'python'")
    return dict(options)


def column_labels(frame: Any, *, positional: bool) -> list[str]:
    """Column names as text; pandas' default integer labels become Column N."""
    if positional:
        return [f"Column {index + 1}" for index in range(len(frame.columns))]
    return [label if isinstance(label, str) else str(label) for label in frame.columns]


def row_text(labels: list[str], values: list[str]) -> str:
    return "\n".join(f"{label}: {value}" for label, value in zip(labels, values))


def is_positional(frame: Any, options: Mapping[str, Any]) -> bool:
    """True when pandas invented 0, 1, 2 ... because there was no header or names."""
    return options.get("header", 0) is None and options.get("names") is None
