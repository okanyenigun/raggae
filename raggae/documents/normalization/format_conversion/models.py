"""Facts about a single image converted into a PDF page."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Literal, Self


class ImageToPdfOutcome(StrEnum):
    CHANGED = "changed"
    FAILED = "failed"


class ImageToPdfFinding(StrEnum):
    UNSUPPORTED_FORMAT = "normalization.image_to_pdf.unsupported_format"
    MULTIPLE_FRAMES = "normalization.image_to_pdf.multiple_frames"
    DECODE_FAILED = "normalization.image_to_pdf.decode_failed"
    ENCODE_FAILED = "normalization.image_to_pdf.encode_failed"


class ImageToPdfIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ImageToPdfFinding
    message: str = Field(min_length=1, pattern=r"\S")


class ImageToPdfReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ImageToPdfOutcome
    image_format: Literal["JPEG", "PNG", "TIFF"] | None = None
    dimensions: tuple[int, int] | None = None
    resolution_dpi: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    transparency_flattened: bool = False
    issues: tuple[ImageToPdfIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.dimensions is not None and any(value <= 0 for value in self.dimensions):
            raise ValueError("image dimensions must be positive")
        if self.outcome is ImageToPdfOutcome.FAILED:
            if not self.issues:
                raise ValueError("a failed report requires an issue")
            if (
                any(
                    value is not None
                    for value in (
                        self.image_format,
                        self.dimensions,
                        self.resolution_dpi,
                    )
                )
                or self.transparency_flattened
            ):
                raise ValueError("a failed report must not claim conversion facts")
        elif self.issues or any(
            value is None
            for value in (
                self.image_format,
                self.dimensions,
                self.resolution_dpi,
            )
        ):
            raise ValueError("success requires format, dimensions, DPI, and no issues")
        return self
