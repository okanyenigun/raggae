"""HTML-specific metadata removal facts, without metadata values."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Self


class HtmlMetadataOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class HtmlMetadataFinding(StrEnum):
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    SANITIZATION_FAILED = "normalization.sanitization.failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class HtmlMetadataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: HtmlMetadataFinding
    message: str = Field(min_length=1, pattern=r"\S")


class HtmlMetadataReport(BaseModel):
    """Number of selected meta tags removed, not a claim of anonymity."""

    model_config = ConfigDict(frozen=True)

    outcome: HtmlMetadataOutcome
    removed_tags: int = Field(default=0, ge=0, strict=True)
    issues: tuple[HtmlMetadataIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is HtmlMetadataOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.issues:
            raise ValueError("only failed may contain issues")
        if self.outcome is HtmlMetadataOutcome.CHANGED:
            if self.removed_tags == 0:
                raise ValueError("changed requires a positive removed_tags count")
        elif self.removed_tags != 0:
            raise ValueError("only changed may report completed removals")
        return self
