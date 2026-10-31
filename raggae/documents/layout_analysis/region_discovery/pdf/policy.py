"""Native PDF layout limits."""

from pydantic import BaseModel, ConfigDict, Field


class PdfLayoutPolicy(BaseModel):
    """Native PDF layout limits."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
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

    max_objects_per_page: int = Field(
        default=10000,
        gt=0,
        description="Maximum parsed objects on one PDF page before region analysis is skipped.",
    )
