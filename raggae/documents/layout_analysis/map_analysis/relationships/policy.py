"""Geometric relationship thresholds and comparison limits."""

from pydantic import BaseModel, ConfigDict, Field


class RegionRelationshipPolicy(BaseModel):
    """Geometric relationship thresholds and comparison limits."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    maximum_caption_gap_ratio: float = Field(
        default=0.04,
        gt=0,
        le=1,
        description="Maximum vertical caption-to-target gap as a fraction of surface height.",
    )

    maximum_caption_height_ratio: float = Field(
        default=0.08,
        gt=0,
        le=1,
        description="Maximum candidate caption height as a fraction of surface height.",
    )

    minimum_horizontal_overlap: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Required horizontal overlap as a fraction of the narrower region width.",
    )

    ambiguity_gap_ratio: float = Field(
        default=0.01,
        gt=0,
        le=1,
        description="Gap difference within which equally labelled caption candidates remain ambiguous.",
    )

    max_regions_per_surface: int = Field(
        default=2000,
        gt=0,
        description="Maximum regions considered on one surface.",
    )

    max_comparisons: int = Field(
        default=100000,
        gt=0,
        description="Maximum region-pair comparisons across the map.",
    )

    max_relationships: int = Field(
        default=10000,
        gt=0,
        description="Maximum relationship links returned.",
    )
