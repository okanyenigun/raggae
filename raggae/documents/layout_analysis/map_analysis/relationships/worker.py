"""Conservative map-only links using geometry and explicit native locators."""

from dataclasses import dataclass
from typing import Literal
from .models import RegionRelationship, RegionRelationshipResult
from .policy import RegionRelationshipPolicy
from ...models import (
    DocxElementLocation,
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    PptxShapeLocation,
    XlsxRangeLocation,
)
from ...utils import cell_bounds


@dataclass(frozen=True, slots=True)
class _CaptionOption:
    caption_id: str
    target_id: str
    gap: float
    priority: int
    basis: Literal["proximity", "shared_parent"]


class RuleBasedRegionRelationshipDetector:
    """Return evidence-labelled suggestions, not semantic matches or merged text."""

    def __init__(self, policy: RegionRelationshipPolicy | None = None) -> None:
        self._policy = policy if policy is not None else RegionRelationshipPolicy()

    @property
    def policy(self) -> RegionRelationshipPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_rule_based_region_relationships"

    def detect(self, layout: LayoutAnalysisResult) -> RegionRelationshipResult:
        if not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult")
        if layout.outcome in ("failed", "not_applicable"):
            return RegionRelationshipResult(
                outcome=layout.outcome, findings=layout.findings
            )
        by_surface: dict[str, list[LayoutRegion]] = {
            surface.id_: [] for surface in layout.surfaces
        }
        for region in layout.regions:
            by_surface[region.surface_id].append(region)
        findings = list(layout.findings)
        links: list[RegionRelationship] = []
        unresolved: set[str] = set()
        comparisons = 0

        def finding(code: str, message: str, surface_id: str | None = None) -> None:
            if not any(
                item.code == f"layout.relationship.{code}"
                and item.surface_id == surface_id
                for item in findings
            ):
                findings.append(
                    LayoutFinding(
                        code=f"layout.relationship.{code}",
                        surface_id=surface_id,
                        message=message,
                    )
                )

        def add(link: RegionRelationship) -> bool:
            if len(links) >= self._policy.max_relationships:
                finding(
                    "relationship_limit",
                    "Further links were not returned: relationship limit reached.",
                )
                unresolved.update((link.source_region_id, link.target_region_id))
                return False
            links.append(link)
            return True

        for surface in layout.surfaces:
            regions = sorted(by_surface[surface.id_], key=lambda region: region.id)
            if len(regions) > self._policy.max_regions_per_surface:
                unresolved.update(
                    region.id
                    for region in regions[self._policy.max_regions_per_surface :]
                )
                regions = regions[: self._policy.max_regions_per_surface]
                finding(
                    "region_limit",
                    "Remaining regions were not compared: per-surface limit reached.",
                    surface.id_,
                )
            for region in regions:
                path_location = isinstance(
                    region.location,
                    (
                        DocxElementLocation,
                        HtmlElementLocation,
                        PptxShapeLocation,
                        XlsxRangeLocation,
                    ),
                )
                if (region.bbox is None and not path_location) or (
                    surface.kind in ("page", "slide") and region.bbox is None
                ):
                    unresolved.add(region.id)
                    finding(
                        "missing_geometry",
                        "Some regions lack enough geometry for spatial relationships; available native containment can still be returned.",
                        surface.id_,
                    )
            options: list[_CaptionOption] = []
            incomplete = False
            for index, first in enumerate(regions):
                for second in regions[index + 1 :]:
                    if comparisons >= self._policy.max_comparisons:
                        incomplete = True
                        break
                    comparisons += 1
                    link = self._containment(first, second)
                    if link is not None and not add(link):
                        incomplete = True
                        break
                    for caption, target in ((first, second), (second, first)):
                        if caption.kind not in (
                            "caption",
                            "text",
                        ) or target.kind not in ("image", "table"):
                            continue
                        if self._shared_html_parent(caption, target):
                            options.append(
                                _CaptionOption(
                                    caption.id, target.id, 0, 0, "shared_parent"
                                )
                            )
                        elif surface.height is not None:
                            gap = self._caption_gap(
                                caption,
                                target,
                                height=surface.height,
                                maximum_gap_ratio=self._policy.maximum_caption_gap_ratio,
                                maximum_height_ratio=self._policy.maximum_caption_height_ratio,
                                minimum_horizontal_overlap=self._policy.minimum_horizontal_overlap,
                            )
                            if gap is not None:
                                options.append(
                                    _CaptionOption(
                                        caption.id,
                                        target.id,
                                        gap,
                                        1 if caption.kind == "caption" else 2,
                                        "proximity",
                                    )
                                )
                if incomplete:
                    break
            if (
                incomplete
                or len(by_surface[surface.id_]) > self._policy.max_regions_per_surface
            ):
                # Do not select a nearest caption from only a subset of its
                # possible competitors. Containment found so far remains useful.
                unresolved.update(region.id for region in by_surface[surface.id_])
                if comparisons >= self._policy.max_comparisons:
                    finding(
                        "comparison_limit",
                        "Caption selection on this surface was skipped: pair-comparison limit reached.",
                        surface.id_,
                    )
                continue
            caption_links, ambiguous = self._caption_links(options)
            if ambiguous:
                unresolved.update(ambiguous)
                finding(
                    "ambiguous_caption",
                    "Several caption/target neighbours are similarly plausible; affected links were not selected.",
                    surface.id_,
                )
            for link in caption_links:
                if not add(link):
                    unresolved.update(region.id for region in regions)
                    break
        return RegionRelationshipResult(
            outcome="partial" if findings or unresolved else "success",
            relationships=tuple(links),
            unresolved_region_ids=tuple(sorted(unresolved)),
            findings=tuple(findings),
        )

    def _caption_links(
        self, options: list[_CaptionOption]
    ) -> tuple[list[RegionRelationship], set[str]]:
        by_caption: dict[str, list[_CaptionOption]] = {}
        by_target: dict[str, list[_CaptionOption]] = {}
        for option in options:
            by_caption.setdefault(option.caption_id, []).append(option)
            by_target.setdefault(option.target_id, []).append(option)
        ambiguous: set[str] = set()

        def choose(
            groups: dict[str, list[_CaptionOption]],
        ) -> dict[str, _CaptionOption]:
            selected = {}
            for identifier, candidates in groups.items():
                best = min(candidates, key=lambda item: (item.priority, item.gap))
                close = [
                    item
                    for item in candidates
                    if item.priority == best.priority
                    and item.gap - best.gap <= self._policy.ambiguity_gap_ratio
                ]
                if len(close) > 1:
                    ambiguous.update(
                        value
                        for item in close
                        for value in (item.caption_id, item.target_id)
                    )
                else:
                    selected[identifier] = best
            return selected

        captions, targets = choose(by_caption), choose(by_target)
        links = [
            RegionRelationship(
                kind="caption_candidate",
                source_region_id=option.caption_id,
                target_region_id=option.target_id,
                basis=option.basis,
            )
            for option in captions.values()
            if targets.get(option.target_id) == option
            and option.caption_id not in ambiguous
            and option.target_id not in ambiguous
        ]
        return links, ambiguous

    @staticmethod
    def _containment(
        first: LayoutRegion, second: LayoutRegion
    ) -> RegionRelationship | None:
        for outer, inner in ((first, second), (second, first)):
            a, b = outer.location, inner.location
            if isinstance(
                a, (DocxElementLocation, HtmlElementLocation, PptxShapeLocation)
            ) and type(a) is type(b):
                if isinstance(a, DocxElementLocation) and isinstance(
                    b, DocxElementLocation
                ):
                    first_path, second_path = a.child_indexes, b.child_indexes
                    same_root = a.part == b.part
                elif isinstance(a, PptxShapeLocation) and isinstance(
                    b, PptxShapeLocation
                ):
                    first_path, second_path = a.shape_path, b.shape_path
                    same_root = a.part == b.part
                elif isinstance(a, HtmlElementLocation) and isinstance(
                    b, HtmlElementLocation
                ):
                    first_path, second_path, same_root = (
                        a.child_indexes,
                        b.child_indexes,
                        True,
                    )
                else:
                    continue
                if (
                    same_root
                    and len(first_path) < len(second_path)
                    and second_path[: len(first_path)] == first_path
                ):
                    return RegionRelationship(
                        kind="contains",
                        source_region_id=outer.id,
                        target_region_id=inner.id,
                        basis="native_ancestry",
                    )
            if (
                isinstance(a, XlsxRangeLocation)
                and isinstance(b, XlsxRangeLocation)
                and a.sheet_name == b.sheet_name
            ):
                a_parts, b_parts = a.cell_range.split(":"), b.cell_range.split(":")
                a1, a2 = cell_bounds(a_parts[0]), cell_bounds(a_parts[-1])
                b1, b2 = cell_bounds(b_parts[0]), cell_bounds(b_parts[-1])
                if (
                    (a1, a2) != (b1, b2)
                    and a1[0] <= b1[0]
                    and a1[1] <= b1[1]
                    and a2[0] >= b2[0]
                    and a2[1] >= b2[1]
                ):
                    return RegionRelationship(
                        kind="contains",
                        source_region_id=outer.id,
                        target_region_id=inner.id,
                        basis="cell_range_containment",
                    )
        for outer, inner in ((first, second), (second, first)):
            a_box, b_box = outer.bbox, inner.bbox
            if (
                a_box is not None
                and b_box is not None
                and a_box != b_box
                and a_box[0] <= b_box[0]
                and a_box[1] <= b_box[1]
                and a_box[2] >= b_box[2]
                and a_box[3] >= b_box[3]
            ):
                return RegionRelationship(
                    kind="contains",
                    source_region_id=outer.id,
                    target_region_id=inner.id,
                    basis="bbox_containment",
                )
        return None

    @staticmethod
    def _caption_gap(
        caption: LayoutRegion,
        target: LayoutRegion,
        *,
        height: float,
        maximum_gap_ratio: float,
        maximum_height_ratio: float,
        minimum_horizontal_overlap: float,
    ) -> float | None:
        a, b = caption.bbox, target.bbox
        if a is None or b is None or a[3] - a[1] > height * maximum_height_ratio:
            return None
        overlap = min(a[2], b[2]) - max(a[0], b[0])
        if overlap < min(a[2] - a[0], b[2] - b[0]) * minimum_horizontal_overlap:
            return None
        if a[1] >= b[3]:
            gap = a[1] - b[3]
        elif b[1] >= a[3]:
            gap = b[1] - a[3]
        else:
            return None
        return gap / height if gap <= height * maximum_gap_ratio else None

    @staticmethod
    def _shared_html_parent(caption: LayoutRegion, target: LayoutRegion) -> bool:
        a, b = caption.location, target.location
        return (
            caption.kind == "caption"
            and isinstance(a, HtmlElementLocation)
            and a.tag == "figcaption"
            and isinstance(b, HtmlElementLocation)
            and len(a.child_indexes) > 1
            and a.child_indexes[:-1] == b.child_indexes[:-1]
        )
