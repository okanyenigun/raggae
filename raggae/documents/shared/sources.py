"""Source and layout-selection arguments shared by text readers and image extractors."""

from collections.abc import Sequence
from pathlib import Path
from ..layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ..normalization.artifacts import Artifact


def as_artifact(source: Path | str | bytes | Artifact) -> Artifact:
    if isinstance(source, bytes):
        return Artifact(data=source)
    if isinstance(source, (Path, str)):
        return Artifact(path=source)
    if not isinstance(source, Artifact):
        raise TypeError("source must be a path, bytes, or Artifact")
    return source


def select_regions(
    layout: LayoutAnalysisResult | None, region_ids: Sequence[str] | None
) -> tuple[LayoutRegion, ...] | None:
    """Validate a selection; None means no layout, all regions when IDs omitted."""
    if layout is not None and not isinstance(layout, LayoutAnalysisResult):
        raise TypeError("layout must be a LayoutAnalysisResult or None")
    if region_ids is not None:
        if layout is None:
            raise ValueError("region_ids require a layout")
        if isinstance(region_ids, (str, bytes)) or not isinstance(region_ids, Sequence):
            raise TypeError("region_ids must be a sequence of strings")
        if any(not isinstance(identifier, str) for identifier in region_ids):
            raise TypeError("region_ids must contain only strings")
        if len(set(region_ids)) != len(region_ids):
            raise ValueError("region_ids must not contain duplicates")
    if layout is None:
        return None
    if layout.outcome not in ("success", "partial"):
        raise ValueError("layout must contain a usable map")
    regions = {region.id: region for region in layout.regions}
    selected = tuple(regions) if region_ids is None else tuple(region_ids)
    if set(selected) - regions.keys():
        raise ValueError("region_ids must belong to the supplied layout")
    return tuple(regions[identifier] for identifier in selected)
