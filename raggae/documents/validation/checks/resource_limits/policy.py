from pydantic import BaseModel, ConfigDict, Field
from enum import StrEnum
from ...schemas.result import Severity


class ResourceFinding(StrEnum):
    """What resource limits can report, across every format."""

    NOT_APPLICABLE = "resource_limits.not_applicable"
    UNREADABLE = "resource_limits.unreadable"

    TOO_MANY_PAGES = "resource_limits.too_many_pages"
    PAGE_TOO_LARGE = "resource_limits.page_too_large"
    IMAGE_TOO_LARGE = "resource_limits.image_too_large"

    TOO_MANY_ENTRIES = "resource_limits.too_many_entries"
    EXPANDS_TOO_LARGE = "resource_limits.expands_too_large"
    EXPANSION_RATIO_TOO_HIGH = "resource_limits.expansion_ratio_too_high"

    TOO_MANY_PIXELS = "resource_limits.too_many_pixels"
    TOO_MANY_FRAMES = "resource_limits.too_many_frames"

    NESTING_TOO_DEEP = "resource_limits.nesting_too_deep"


def _default_resource_severities() -> dict[str, Severity]:
    return {
        ResourceFinding.UNREADABLE: Severity.REJECT,
        ResourceFinding.TOO_MANY_PAGES: Severity.REJECT,
        ResourceFinding.PAGE_TOO_LARGE: Severity.REJECT,
        ResourceFinding.IMAGE_TOO_LARGE: Severity.REJECT,
        ResourceFinding.TOO_MANY_ENTRIES: Severity.REJECT,
        ResourceFinding.EXPANDS_TOO_LARGE: Severity.REJECT,
        ResourceFinding.EXPANSION_RATIO_TOO_HIGH: Severity.REJECT,
        ResourceFinding.TOO_MANY_PIXELS: Severity.REJECT,
        ResourceFinding.TOO_MANY_FRAMES: Severity.REJECT,
        ResourceFinding.NESTING_TOO_DEEP: Severity.REJECT,
        ResourceFinding.NOT_APPLICABLE: Severity.INFO,
    }


class PdfLimits(BaseModel):
    """What a PDF may declare about itself before it is refused."""

    model_config = ConfigDict(frozen=True)

    max_pages: int = Field(
        default=2_000,
        gt=0,
        description="Pages in the document. Every later stage is per-page work.",
    )

    max_page_points: float = Field(
        default=20_000,
        gt=0,
        description=(
            "Longest page edge in PDF points. A page can declare itself 200 metres "
            "wide, and rendering it later allocates for that."
        ),
    )

    max_image_pixels: int = Field(
        default=100_000_000,
        gt=0,
        description=(
            "Pixels in any one embedded image, taken from the image's own header."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=_default_resource_severities,
        description="Default decision severity for resource-limit findings.",
    )


class ArchiveLimits(BaseModel):
    """What an OOXML archive may declare about itself before it is refused."""

    model_config = ConfigDict(frozen=True)

    max_entries: int = Field(
        default=1_000,
        gt=0,
        description="Files inside the archive. A document is tens of parts.",
    )

    max_uncompressed_bytes: int = Field(
        default=1_000_000_000,
        gt=0,
        description=(
            "Total size once expanded, as the archive declares it — read from the "
            "central directory without extracting anything."
        ),
    )

    max_expansion_ratio: float = Field(
        default=100.0,
        gt=1,
        description=(
            "Uncompressed divided by compressed. Catches a bomb that stays under "
            "the byte limit by being tiny to begin with."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=_default_resource_severities,
        description="Default decision severity for resource-limit findings.",
    )


class ImageLimits(BaseModel):
    """What an image may declare about itself before it is refused."""

    model_config = ConfigDict(frozen=True)

    max_pixels: int = Field(
        default=100_000_000,
        gt=0,
        description=(
            "Width × height from the header. Decoding allocates roughly four bytes "
            "per pixel regardless of how small the file is."
        ),
    )

    max_frames: int = Field(
        default=100,
        gt=0,
        description="Frames in a multi-page TIFF, each carrying its own cost.",
    )
    severities: dict[str, Severity] = Field(
        default_factory=_default_resource_severities,
        description="Default decision severity for resource-limit findings.",
    )


class TextLimits(BaseModel):
    """What a text document may contain before it is refused."""

    model_config = ConfigDict(frozen=True)

    max_nesting_depth: int = Field(
        default=100,
        gt=0,
        description=(
            "Nesting in json and jsonl. Deep nesting exhausts the parser's stack "
            "with no exploit involved, just brackets."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=_default_resource_severities,
        description="Default decision severity for resource-limit findings.",
    )
