"""Whitespace-cut ordering under an explicit column-reading convention."""

from dataclasses import dataclass
from typing import Literal
from .policy import ReadingOrderPolicy
from .models import ReadingOrderEdge, ReadingOrderResult
from ...models import BoundingBox, LayoutAnalysisResult, LayoutFinding

type Axis = Literal["x", "y"]
type Basis = Literal["vertical_gap", "horizontal_gap", "page_order"]


@dataclass(frozen=True, slots=True)
class _Box:
    id: str
    bbox: BoundingBox


@dataclass(frozen=True, slots=True)
class _Partition:
    # A singleton leaf is located in sequence; a larger leaf has no internal order.
    groups: tuple[tuple[str, ...], ...]
    boundaries: tuple[Basis, ...] = ()


class GeometricReadingOrderResolver:
    """Suggest column-first order from boxes; do not infer semantic relationships.

    Prefer clear vertical gutters. When a spanning region blocks a gutter, peel
    off its horizontal band, then search again. Remaining inseparable groups stay
    unordered. Every returned edge is a geometric suggestion, not a truth claim.
    """

    def __init__(self, policy: ReadingOrderPolicy | None = None) -> None:
        self._policy = policy if policy is not None else ReadingOrderPolicy()

    @property
    def policy(self) -> ReadingOrderPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_geometric_reading_order"

    def resolve_order(
        self,
        layout: LayoutAnalysisResult,
        *,
        direction: Literal["ltr", "rtl"] = "ltr",
    ) -> ReadingOrderResult:
        if not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult")
        if direction not in ("ltr", "rtl"):
            raise ValueError("direction must be 'ltr' or 'rtl'")
        if layout.outcome in ("failed", "not_applicable"):
            return ReadingOrderResult(outcome=layout.outcome, findings=layout.findings)

        if all(
            surface.width is None and surface.height is None
            for surface in layout.surfaces
        ):
            return ReadingOrderResult(
                outcome="not_applicable",
                findings=layout.findings
                + (
                    LayoutFinding(
                        code="layout.order.no_geometry",
                        message="Native document structure has no page rectangles; geometric ordering does not apply.",
                    ),
                ),
            )
        by_surface: dict[str, list[_Box]] = {
            surface.id_: [] for surface in layout.surfaces
        }
        unlocated = []
        for region in layout.regions:
            if region.bbox is None:
                unlocated.append(region.id)
            else:
                by_surface[region.surface_id].append(_Box(region.id, region.bbox))
        groups: list[tuple[str, ...]] = []
        boundaries: list[Basis] = []
        findings = list(layout.findings)
        if unlocated:
            findings.append(
                LayoutFinding(
                    code="layout.order.no_geometry",
                    message="Some regions have no page rectangles and remain unordered.",
                )
            )
        for surface in layout.surfaces:
            regions = tuple(by_surface[surface.id_])
            if not regions:
                continue
            assert surface.width is not None and surface.height is not None
            partition = self._partition(
                regions,
                surface.width * self._policy.minimum_gap_ratio,
                surface.height * self._policy.minimum_gap_ratio,
                direction,
                depth=0,
            )
            if any(len(group) > 1 for group in partition.groups):
                findings.append(
                    LayoutFinding(
                        code="layout.order.unresolved",
                        surface_id=surface.id_,
                        message="Some regions could not be separated by the geometric rules; their internal order is unresolved.",
                    )
                )
            if groups:
                boundaries.append("page_order")
            groups.extend(partition.groups)
            boundaries.extend(partition.boundaries)

        edges: list[ReadingOrderEdge] = []
        for before, after, basis in zip(groups, groups[1:], boundaries):
            # Avoid quadratic all-to-all links between two unresolved clusters.
            # Their members already remain explicitly unresolved.
            if len(before) > 1 and len(after) > 1:
                continue
            edges.extend(
                ReadingOrderEdge(
                    before_region_id=first, after_region_id=second, basis=basis
                )
                for first in before
                for second in after
            )
        unresolved = tuple(
            identifier for group in groups if len(group) > 1 for identifier in group
        ) + tuple(unlocated)
        return ReadingOrderResult(
            outcome="partial" if findings or layout.outcome == "partial" else "success",
            edges=tuple(edges),
            unresolved_region_ids=unresolved,
            findings=tuple(findings),
        )

    def _partition(
        self,
        regions: tuple[_Box, ...],
        x_gap: float,
        y_gap: float,
        direction: Literal["ltr", "rtl"],
        *,
        depth: int,
    ) -> _Partition:
        if len(regions) == 1:
            return _Partition(((regions[0].id,),))
        # Bound recursive splitting; reaching the limit produces an unresolved
        # group, using the same honest partial-result contract as an overlap.
        if depth >= self._policy.max_partition_depth:
            return _Partition((tuple(sorted(region.id for region in regions)),))
        vertical = self._gaps(regions, "x", x_gap)
        if vertical:
            _, start, end = max(vertical, key=lambda gap: (gap[0], -gap[1]))
            axis: Axis = "x"
        else:
            horizontal = self._gaps(regions, "y", y_gap)
            if not horizontal:
                return _Partition((tuple(sorted(region.id for region in regions)),))
            width = max(region.bbox[2] for region in regions) - min(
                region.bbox[0] for region in regions
            )
            spanning = [
                region
                for region in regions
                if region.bbox[2] - region.bbox[0]
                >= width * self._policy.minimum_spanning_width_ratio
            ]
            # A gap touching a broad block can separate a title/table/footer
            # band from columns. No semantic label is assigned to that block.
            band_gaps = [
                gap
                for gap in horizontal
                if any(
                    abs(gap[1] - region.bbox[3]) < 1e-8
                    or abs(gap[2] - region.bbox[1]) < 1e-8
                    for region in spanning
                )
            ]
            _, start, end = (
                min(band_gaps, key=lambda gap: gap[1]) if band_gaps else max(horizontal)
            )
            axis = "y"
        coordinate = (start + end) / 2
        index = 0 if axis == "x" else 1
        first = tuple(
            region for region in regions if region.bbox[index + 2] <= coordinate
        )
        second = tuple(region for region in regions if region.bbox[index] >= coordinate)
        if axis == "x" and direction == "rtl":
            first, second = second, first
        left = self._partition(first, x_gap, y_gap, direction, depth=depth + 1)
        right = self._partition(second, x_gap, y_gap, direction, depth=depth + 1)
        basis: Basis = "vertical_gap" if axis == "x" else "horizontal_gap"
        return _Partition(
            left.groups + right.groups, left.boundaries + (basis,) + right.boundaries
        )

    @staticmethod
    def _gaps(
        regions: tuple[_Box, ...],
        axis: Axis,
        minimum: float,
    ) -> list[tuple[float, float, float]]:
        index = 0 if axis == "x" else 1
        intervals = sorted(
            (region.bbox[index], region.bbox[index + 2]) for region in regions
        )
        end = intervals[0][1]
        gaps = []
        for start, following_end in intervals[1:]:
            if start - end >= minimum:
                gaps.append((start - end, end, start))
            end = max(end, following_end)
        return gaps
