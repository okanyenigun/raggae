"""PDF active-content removal facts, separate from the shared document result."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import (
    BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator,
)


class PdfActiveContentOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class PdfActiveContentCategory(StrEnum):
    SCRIPT = "script"
    AUTOMATIC_ACTION = "automatic_action"
    LAUNCH_ACTION = "launch_action"
    EMBEDDED_FILE = "embedded_file"
    RICH_MEDIA = "rich_media"
    REMOTE_REFERENCE = "remote_reference"


class PdfActiveContentFinding(StrEnum):
    PASSWORD_REQUIRED = "normalization.sanitization.password_required"
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    SANITIZATION_FAILED = "normalization.sanitization.failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class PdfActiveContentIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: PdfActiveContentFinding
    message: str = Field(min_length=1, pattern=r"\S")


class PdfActiveContentReport(BaseModel):
    """Counts of removed structures, not a guarantee of document safety."""

    model_config = ConfigDict(frozen=True)

    outcome: PdfActiveContentOutcome
    page_count: int | None = Field(default=None, ge=0, strict=True)
    removals: Mapping[
        PdfActiveContentCategory, Annotated[int, Field(gt=0, strict=True)]
    ] = Field(default_factory=dict, validate_default=True)
    issues: tuple[PdfActiveContentIssue, ...] = ()

    @field_validator("removals", mode="after")
    @classmethod
    def _freeze_removals(
        cls, value: Mapping[PdfActiveContentCategory, int]
    ) -> Mapping[PdfActiveContentCategory, int]:
        return MappingProxyType(dict(value))

    @field_serializer("removals")
    def _serialize_removals(
        self, value: Mapping[PdfActiveContentCategory, int]
    ) -> dict[str, int]:
        return {category.value: count for category, count in value.items()}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is PdfActiveContentOutcome.FAILED:
            if not self.issues:
                raise ValueError("a failed report requires an issue")
        elif self.page_count is None or self.issues:
            raise ValueError("success requires page count and no issues")
        if self.outcome is PdfActiveContentOutcome.CHANGED:
            if not self.removals:
                raise ValueError("changed requires at least one removal")
        elif self.removals:
            raise ValueError("only changed may report completed removals")
        return self
