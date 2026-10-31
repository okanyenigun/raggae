"""Slide layout and group traversal limits."""

from pydantic import BaseModel, ConfigDict, Field


class PptxLayoutPolicy(BaseModel):
    """Slide layout and group traversal limits."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    max_source_bytes: int = Field(
        default=50 * 1024 * 1024,
        gt=0,
        description="Largest source file or in-memory input, in bytes.",
    )

    max_expanded_bytes: int = Field(
        default=100 * 1024 * 1024,
        gt=0,
        description="Maximum total declared uncompressed size of Office ZIP parts.",
    )

    max_slides: int = Field(
        default=100,
        gt=0,
        description="Maximum slides to analyze.",
    )

    max_shapes_per_slide: int = Field(
        default=2000,
        gt=0,
        description="Maximum shapes visited on one slide, including supported group children.",
    )

    max_archive_entries: int = Field(
        default=2_000,
        gt=0,
        description="Maximum Office ZIP entries before the package is refused.",
    )

    max_group_depth: int = Field(
        default=32,
        gt=0,
        description="Shape path length at which nested slide group traversal stops.",
    )
