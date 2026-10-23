"""Serializable, read-only results specific to text encoding normalization."""

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
from typing import Self, TypeAlias


TextEncodingDetailValue: TypeAlias = str | int | bool | None


class TextEncodingOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class TextEncodingFinding(StrEnum):
    ENCODING_REQUIRED = "normalization.text.encoding_required"
    DECODE_FAILED = "normalization.text.decode_failed"
    ENCODE_FAILED = "normalization.text.encode_failed"


class TextEncodingIssue(BaseModel):
    """An encoding problem, without source text or raw decoder diagnostics."""

    model_config = ConfigDict(frozen=True)

    code: TextEncodingFinding
    message: str = Field(min_length=1)
    detail: Mapping[str, TextEncodingDetailValue] = Field(
        default_factory=dict, validate_default=True
    )

    @field_validator("message")
    @classmethod
    def _require_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text encoding issue message cannot be blank")
        return value

    @field_validator("detail", mode="after")
    @classmethod
    def _freeze_detail(
        cls, value: Mapping[str, TextEncodingDetailValue]
    ) -> Mapping[str, TextEncodingDetailValue]:
        return MappingProxyType(dict(value))

    @field_serializer("detail")
    def _serialize_detail(
        self, value: Mapping[str, TextEncodingDetailValue]
    ) -> dict[str, TextEncodingDetailValue]:
        return dict(value)


class TextEncodingReport(BaseModel):
    """Serializable information about the transformation, separate from content."""

    model_config = ConfigDict(frozen=True)

    outcome: TextEncodingOutcome
    source_encoding: str | None = None
    target_encoding: str = "utf-8"
    bom_removed: bool = False
    issues: tuple[TextEncodingIssue, ...] = ()

    @field_validator("source_encoding")
    @classmethod
    def _require_encoding_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("source_encoding cannot be blank")
        return value.strip()

    @field_validator("target_encoding")
    @classmethod
    def _require_target_encoding(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target_encoding cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def _require_consistent_outcome(self) -> Self:
        if self.outcome is TextEncodingOutcome.FAILED:
            if not self.issues:
                raise ValueError("a failed result requires at least one issue")
        else:
            if self.source_encoding is None:
                raise ValueError("a successful result requires the source encoding")
            if self.issues:
                raise ValueError("only a failed result may contain issues")

        if self.bom_removed and self.outcome is not TextEncodingOutcome.CHANGED:
            raise ValueError("only a changed result may report a removed BOM")
        if self.outcome is TextEncodingOutcome.NO_CHANGE:
            equivalent_sources = {
                "utf-16": {"utf-16-le", "utf-16-be"},
                "utf-32": {"utf-32-le", "utf-32-be"},
            }
            if (
                self.source_encoding != self.target_encoding
                and self.source_encoding
                not in equivalent_sources.get(self.target_encoding, set())
            ):
                raise ValueError(
                    "a no-change result requires a compatible target encoding"
                )
        return self
