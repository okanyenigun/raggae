"""Selected image metadata removal facts, without private metadata values."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Literal, Self


class ImageMetadataOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class ImageMetadataFinding(StrEnum):
    UNSUPPORTED_FORMAT = "normalization.sanitization.unsupported_format"
    MULTIPLE_FRAMES = "normalization.sanitization.multiple_frames"
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class ImageMetadataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ImageMetadataFinding
    message: str = Field(min_length=1, pattern=r"\S")


class ImageMetadataReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ImageMetadataOutcome
    image_format: Literal["JPEG", "PNG", "TIFF"] | None = None
    dimensions: tuple[int, int] | None = None
    removed_categories: tuple[Literal["exif", "xmp", "comment", "png_text"], ...] = ()
    issues: tuple[ImageMetadataIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.dimensions is not None and any(value <= 0 for value in self.dimensions):
            raise ValueError("dimensions must be positive")
        if len(set(self.removed_categories)) != len(self.removed_categories):
            raise ValueError("removed_categories cannot contain duplicates")
        if self.outcome is ImageMetadataOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.image_format is None or self.dimensions is None or self.issues:
            raise ValueError("success requires image format, dimensions, and no issues")
        if self.outcome is ImageMetadataOutcome.CHANGED:
            if not self.removed_categories:
                raise ValueError("changed requires removed metadata categories")
        elif self.removed_categories:
            raise ValueError("only changed may report completed removals")
        return self
