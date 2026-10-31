"""Raster decoding, PDF rendering, and inference output limits."""

from pydantic import BaseModel, ConfigDict, Field


class VisualLayoutPolicy(BaseModel):
    """Raster decoding, PDF rendering, and inference output limits."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    dpi: int = Field(
        default=144,
        gt=0,
        description="Resolution used when rendering PDF pages for inference.",
    )

    max_source_bytes: int = Field(
        default=50 * 1024 * 1024,
        gt=0,
        description="Largest source file or in-memory input, in bytes.",
    )

    max_pages: int = Field(
        default=100,
        gt=0,
        description="Maximum PDF pages or image frames to analyze.",
    )

    max_pixels_per_page: int = Field(
        default=20000000,
        gt=0,
        description="Maximum decoded image or rendered PDF pixels per surface.",
    )

    max_regions_per_page: int = Field(
        default=2000,
        gt=0,
        description="Maximum model detections processed for one surface.",
    )
