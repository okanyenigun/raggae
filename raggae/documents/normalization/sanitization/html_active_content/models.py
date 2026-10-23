"""HTML-specific removal facts using the shared normalization result container."""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Self


class HtmlActiveContentOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class HtmlActiveContentCategory(StrEnum):
    SCRIPT = "script"
    EMBEDDED_FRAME = "embedded_frame"
    EVENT_HANDLER = "event_handler"
    REMOTE_REFERENCE = "remote_reference"


class HtmlActiveContentFinding(StrEnum):
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    SANITIZATION_FAILED = "normalization.sanitization.failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class HtmlActiveContentIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: HtmlActiveContentFinding
    message: str = Field(min_length=1, pattern=r"\S")


class HtmlActiveContentReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: HtmlActiveContentOutcome
    removals: Mapping[
        HtmlActiveContentCategory, Annotated[int, Field(gt=0, strict=True)]
    ] = Field(default_factory=dict, validate_default=True)
    issues: tuple[HtmlActiveContentIssue, ...] = ()

    @field_validator("removals", mode="after")
    @classmethod
    def _freeze_removals(
        cls, value: Mapping[HtmlActiveContentCategory, int]
    ) -> Mapping[HtmlActiveContentCategory, int]:
        return MappingProxyType(dict(value))

    @field_serializer("removals")
    def _serialize_removals(
        self, value: Mapping[HtmlActiveContentCategory, int]
    ) -> dict[str, int]:
        return {category.value: count for category, count in value.items()}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is HtmlActiveContentOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.issues:
            raise ValueError("only failed may contain issues")
        if self.outcome is HtmlActiveContentOutcome.CHANGED:
            if not self.removals:
                raise ValueError("changed requires removals")
        elif self.removals:
            raise ValueError("only changed may report completed removals")
        return self
