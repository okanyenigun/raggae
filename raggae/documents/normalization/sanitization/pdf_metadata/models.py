"""PDF metadata removal facts, separate from the shared document result."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Self


class PdfMetadataOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class PdfMetadataFinding(StrEnum):
    PASSWORD_REQUIRED = "normalization.sanitization.password_required"
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    SANITIZATION_FAILED = "normalization.sanitization.failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class PdfMetadataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: PdfMetadataFinding
    message: str = Field(min_length=1, pattern=r"\S")


class PdfMetadataReport(BaseModel):
    """Which standard metadata structures were removed, never their contents."""

    model_config = ConfigDict(frozen=True)

    outcome: PdfMetadataOutcome
    document_info_removed: bool = False
    xmp_removed: bool = False
    page_count: int | None = Field(default=None, ge=0, strict=True)
    issues: tuple[PdfMetadataIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is PdfMetadataOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.page_count is None or self.issues:
            raise ValueError("success requires page count and no issues")
        removed = self.document_info_removed or self.xmp_removed
        if self.outcome is PdfMetadataOutcome.CHANGED:
            if not removed:
                raise ValueError("changed requires at least one metadata removal")
        elif removed:
            raise ValueError("only changed may report completed metadata removals")
        return self
