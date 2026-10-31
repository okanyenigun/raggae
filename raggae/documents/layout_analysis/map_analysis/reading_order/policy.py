"""Geometric reading-order thresholds."""

from pydantic import BaseModel, ConfigDict, Field


class ReadingOrderPolicy(BaseModel):
    """Geometric reading-order thresholds."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    minimum_gap_ratio: float = Field(
        default=0.005,
        gt=0,
        lt=1,
        description="Minimum separating gap as a fraction of surface width or height.",
    )

    minimum_spanning_width_ratio: float = Field(
        default=0.7,
        gt=0,
        le=1,
        description="Region width fraction needed to prefer an adjacent horizontal band gap.",
    )

    max_partition_depth: int = Field(
        default=64,
        gt=0,
        description="Maximum recursive partition depth before a group remains unresolved.",
    )
