"""Word body layout limits."""

from pydantic import BaseModel, ConfigDict, Field


class DocxLayoutPolicy(BaseModel):
    """Word body layout limits."""

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

    max_regions: int = Field(
        default=10000,
        gt=0,
        description="Maximum regions returned for the document.",
    )

    max_archive_entries: int = Field(
        default=2_000,
        gt=0,
        description="Maximum Office ZIP entries before the package is refused.",
    )
