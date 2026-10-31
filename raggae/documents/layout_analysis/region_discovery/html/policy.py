"""HTML structure traversal limits."""

from pydantic import BaseModel, ConfigDict, Field


class HtmlLayoutPolicy(BaseModel):
    """HTML structure traversal limits."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    max_source_bytes: int = Field(
        default=50 * 1024 * 1024,
        gt=0,
        description="Largest source file or in-memory input, in bytes.",
    )

    max_elements: int = Field(
        default=50000,
        gt=0,
        description="Maximum HTML elements visited after parsing.",
    )

    max_depth: int = Field(
        default=128,
        gt=0,
        description="Maximum HTML element nesting depth visited.",
    )

    max_regions: int = Field(
        default=10000,
        gt=0,
        description="Maximum regions returned for the document.",
    )
