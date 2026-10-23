"""Facts from a declared Office-document-to-PDF conversion."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Literal, Self


OfficeSourceFormat = Literal["docx", "xlsx", "pptx"]


class OfficeToPdfOutcome(StrEnum):
    CHANGED = "changed"
    FAILED = "failed"


class OfficeToPdfFinding(StrEnum):
    DEPENDENCY_UNAVAILABLE = "normalization.office_to_pdf.dependency_unavailable"
    TIMEOUT = "normalization.office_to_pdf.timeout"
    CONVERSION_FAILED = "normalization.office_to_pdf.conversion_failed"
    INVALID_OUTPUT = "normalization.office_to_pdf.invalid_output"


class OfficeToPdfIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: OfficeToPdfFinding
    message: str = Field(min_length=1, pattern=r"\S")


class OfficeToPdfReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: OfficeToPdfOutcome
    source_format: OfficeSourceFormat
    output_size_bytes: int | None = Field(default=None, gt=0, strict=True)
    issues: tuple[OfficeToPdfIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is OfficeToPdfOutcome.FAILED:
            if not self.issues or self.output_size_bytes is not None:
                raise ValueError("failure requires an issue and no output size")
        elif self.issues or self.output_size_bytes is None:
            raise ValueError("success requires an output size and no issues")
        return self
