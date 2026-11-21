"""Cell boxes (min column, min row, max column, max row) and how they relate."""

from collections.abc import Callable
from ...layout_analysis.models import LayoutRegion

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
