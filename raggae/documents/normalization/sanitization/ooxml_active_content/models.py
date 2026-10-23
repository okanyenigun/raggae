"""OOXML-specific removal facts using the shared normalization result container."""

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
from typing import Annotated, Literal, Self


class OoxmlActiveContentOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class OoxmlActiveContentCategory(StrEnum):
    MACRO = "macro"
    REMOTE_REFERENCE = "remote_reference"
    REMOTE_TEMPLATE = "remote_template"


class OoxmlActiveContentFinding(StrEnum):
    UNSUPPORTED_FORMAT = "normalization.sanitization.unsupported_format"
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class OoxmlActiveContentIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: OoxmlActiveContentFinding
    message: str = Field(min_length=1, pattern=r"\S")


class OoxmlActiveContentReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: OoxmlActiveContentOutcome
    document_format: Literal["docx", "xlsx", "pptx"] | None = None
    removals: Mapping[
        OoxmlActiveContentCategory, Annotated[int, Field(gt=0, strict=True)]
    ] = Field(default_factory=dict, validate_default=True)
    issues: tuple[OoxmlActiveContentIssue, ...] = ()

    @field_validator("removals", mode="after")
    @classmethod
    def _freeze_removals(
        cls, value: Mapping[OoxmlActiveContentCategory, int]
    ) -> Mapping[OoxmlActiveContentCategory, int]:
        return MappingProxyType(dict(value))

    @field_serializer("removals")
    def _serialize_removals(
        self, value: Mapping[OoxmlActiveContentCategory, int]
    ) -> dict[str, int]:
        return {category.value: count for category, count in value.items()}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is OoxmlActiveContentOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.document_format is None or self.issues:
            raise ValueError("success requires document format and no issues")
        if self.outcome is OoxmlActiveContentOutcome.CHANGED:
            if not self.removals:
                raise ValueError("changed requires removals")
        elif self.removals:
            raise ValueError("only changed may report completed removals")
        return self
