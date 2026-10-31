"""Repeated PDF margin matching thresholds and limits."""

from pydantic import BaseModel, ConfigDict, Field


class RepeatedMarginPolicy(BaseModel):
    """Repeated PDF margin matching thresholds and limits."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)

    margin_ratio: float = Field(
        default=0.12, gt=0, le=0.5,
        description='Top/bottom page-height fraction eligible for margin matching.',
    )

    position_tolerance: float = Field(
        default=0.03, gt=0, le=1,
        description='Maximum normalized center displacement from a matching cluster reference.',
    )

    minimum_pages: int = Field(
        default=2, ge=2,
        description='Minimum distinct pages supporting a repeated-margin group.',
    )

    minimum_page_fraction: float = Field(
        default=0.6, gt=0, le=1,
        description='Minimum fraction of supplied PDF surfaces supporting a margin group.',
    )

    max_source_bytes: int = Field(
        default=50 * 1024 * 1024, gt=0,
        description='Largest source file or in-memory input, in bytes.',
    )

    max_pages: int = Field(
        default=100, gt=0,
        description='Maximum PDF pages or image frames to analyze.',
    )

    max_objects_per_page: int = Field(
        default=10000, gt=0,
        description='Maximum parsed objects on one PDF page before region analysis is skipped.',
    )

    max_margin_regions: int = Field(
        default=2000, gt=0,
        description='Maximum candidate text or heading regions compared across pages.',
    )
