"""Report for an explicitly selected document-to-HTML conversion."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Literal, Self


HtmlSourceFormat = Literal["docx", "odt", "rtf", "epub", "markdown"]


class DocumentToHtmlOutcome(StrEnum):
    CHANGED = "changed"
    FAILED = "failed"


class DocumentToHtmlFinding(StrEnum):
    DEPENDENCY_UNAVAILABLE = "normalization.document_to_html.dependency_unavailable"
    TIMEOUT = "normalization.document_to_html.timeout"
    CONVERSION_FAILED = "normalization.document_to_html.conversion_failed"
    INVALID_OUTPUT = "normalization.document_to_html.invalid_output"


class DocumentToHtmlIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: DocumentToHtmlFinding
    message: str = Field(min_length=1, pattern=r"\S")


class DocumentToHtmlReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: DocumentToHtmlOutcome
    source_format: HtmlSourceFormat
    output_size_bytes: int | None = Field(default=None, gt=0, strict=True)
    issues: tuple[DocumentToHtmlIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is DocumentToHtmlOutcome.FAILED:
            if not self.issues or self.output_size_bytes is not None:
                raise ValueError("failure requires an issue and no output size")
        elif self.issues or self.output_size_bytes is None:
            raise ValueError("success requires an output size and no issues")
        return self
