"""Serializable PDF access facts, never passwords or raw parser diagnostics."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Self


class PdfAccessOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class PdfAccessFinding(StrEnum):
    PASSWORD_REQUIRED = "normalization.pdf.password_required"
    DECRYPT_FAILED = "normalization.pdf.decrypt_failed"
    DECODE_FAILED = "normalization.pdf.decode_failed"
    ENCODE_FAILED = "normalization.pdf.encode_failed"


class PdfAccessIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: PdfAccessFinding
    message: str = Field(min_length=1, pattern=r"\S")


class PdfAccessReport(BaseModel):
    """What access preparation did, separate from the document and its secret."""

    model_config = ConfigDict(frozen=True)

    outcome: PdfAccessOutcome
    source_encrypted: bool | None = None
    password_required: bool | None = None
    page_count: int | None = Field(default=None, ge=0, strict=True)
    issues: tuple[PdfAccessIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.source_encrypted is False and self.password_required is True:
            raise ValueError("an unencrypted PDF cannot require a password")
        if self.password_required is not None and self.source_encrypted is None:
            raise ValueError("password_required requires a known encryption state")
        if self.outcome is PdfAccessOutcome.FAILED:
            if not self.issues:
                raise ValueError("a failed report requires an issue")
        else:
            if self.issues:
                raise ValueError("only failed reports may contain issues")
            if self.source_encrypted is None or self.password_required is None:
                raise ValueError("success requires PDF access facts")
            if self.page_count is None:
                raise ValueError("success requires a page count")
        if self.outcome is PdfAccessOutcome.CHANGED:
            if self.source_encrypted is not True or self.password_required is not True:
                raise ValueError(
                    "changed requires a password-required encrypted source"
                )
        if self.outcome is PdfAccessOutcome.NO_CHANGE and self.password_required:
            raise ValueError("a password-required PDF cannot be no_change")
        return self
