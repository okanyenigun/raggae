"""Worksheet layout limits."""

from pydantic import BaseModel, ConfigDict, Field


class XlsxLayoutPolicy(BaseModel):
    """Worksheet layout limits."""

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

    max_sheets: int = Field(
        default=100,
        gt=0,
        description="Maximum worksheets to analyze.",
    )

    max_cells_per_sheet: int = Field(
        default=100000,
        gt=0,
        description="Maximum nonempty cells processed on one worksheet.",
    )

    max_regions_per_sheet: int = Field(
        default=2000,
        gt=0,
        description="Maximum regions returned for one worksheet.",
    )

    max_archive_entries: int = Field(
        default=2_000,
        gt=0,
        description="Maximum Office ZIP entries before the package is refused.",
    )
