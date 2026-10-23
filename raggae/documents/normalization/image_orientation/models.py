"""Serializable image-specific facts; requests and document results are shared."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Literal, Self


class ImageOrientationOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class ImageOrientationFinding(StrEnum):
    UNSUPPORTED_FORMAT = "normalization.image.unsupported_format"
    MULTIPLE_FRAMES = "normalization.image.multiple_frames"
    INVALID_ORIENTATION = "normalization.image.invalid_orientation"
    DECODE_FAILED = "normalization.image.decode_failed"
    ENCODE_FAILED = "normalization.image.encode_failed"


class ImageOrientationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ImageOrientationFinding
    message: str = Field(min_length=1, pattern=r"\S")


ImageOrientationOperation = Literal[
    "mirror_horizontal",
    "rotate_180",
    "mirror_vertical",
    "transpose",
    "rotate_90_clockwise",
    "transverse",
    "rotate_90_counterclockwise",
]

_OPERATIONS: dict[int, ImageOrientationOperation] = {
    2: "mirror_horizontal",
    3: "rotate_180",
    4: "mirror_vertical",
    5: "transpose",
    6: "rotate_90_clockwise",
    7: "transverse",
    8: "rotate_90_counterclockwise",
}


class ImageOrientationReport(BaseModel):
    """Transformation facts, without image bytes or storage lifecycle metadata."""

    model_config = ConfigDict(frozen=True)

    outcome: ImageOrientationOutcome
    image_format: Literal["JPEG", "PNG", "TIFF"] | None = None
    source_orientation: int | None = Field(default=None, ge=1, le=8, strict=True)
    applied_operation: ImageOrientationOperation | None = None
    input_dimensions: tuple[int, int] | None = None
    output_dimensions: tuple[int, int] | None = None
    issues: tuple[ImageOrientationIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for dimensions in (self.input_dimensions, self.output_dimensions):
            if dimensions is not None and any(value <= 0 for value in dimensions):
                raise ValueError("image dimensions must be positive")
        if self.outcome is ImageOrientationOutcome.FAILED:
            if not self.issues:
                raise ValueError("a failed report requires an issue")
        elif self.issues or self.image_format is None or self.input_dimensions is None:
            raise ValueError("success requires format, dimensions, and no issues")

        if self.outcome is ImageOrientationOutcome.CHANGED:
            orientation = self.source_orientation
            if orientation is None or orientation not in _OPERATIONS:
                raise ValueError("changed requires a transforming orientation")
            if self.applied_operation != _OPERATIONS[orientation]:
                raise ValueError("operation must match orientation")
            assert self.input_dimensions is not None
            expected = self.input_dimensions
            if orientation >= 5:
                expected = (expected[1], expected[0])
            if self.output_dimensions != expected:
                raise ValueError("output dimensions must match the operation")
        elif self.applied_operation is not None or self.output_dimensions is not None:
            raise ValueError(
                "only changed may report an operation or output dimensions"
            )
        if self.outcome is ImageOrientationOutcome.NO_CHANGE:
            if self.source_orientation not in (None, 1):
                raise ValueError("no_change requires missing or normal orientation")
        return self
