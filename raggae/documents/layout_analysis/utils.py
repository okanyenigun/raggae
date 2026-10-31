"""Small source-input and coordinate helpers shared by layout workers."""

import re
from pathlib import Path
from ..normalization.artifacts import Artifact


def resolve_source(source: Path | str | bytes | Artifact) -> Artifact:
    """Keep an artifact or wrap a direct path/bytes input without opening it."""
    if isinstance(source, Artifact):
        return source
    if isinstance(source, bytes):
        return Artifact(data=source)
    if isinstance(source, (Path, str)):
        return Artifact(path=source)
    raise TypeError("source must be a path, bytes, or Artifact")


def cell_bounds(reference: str) -> tuple[int, int]:
    """Parse one canonical A1 reference within Excel's worksheet limits."""
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", reference)
    if match is None:
        raise ValueError("cell references must use uppercase A1 notation")
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(match[2])
    if column > 16_384 or row > 1_048_576:
        raise ValueError("cell reference exceeds worksheet bounds")
    return column, row
