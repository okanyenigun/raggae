"""Standard OOXML property-part removal facts, without property values."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OoxmlMetadataOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class OoxmlMetadataFinding(StrEnum):
    UNSUPPORTED_FORMAT = "normalization.sanitization.unsupported_format"
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class OoxmlMetadataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: OoxmlMetadataFinding
    message: str = Field(min_length=1, pattern=r"\S")


class OoxmlMetadataReport(BaseModel):
    """Which standard parts were removed, not anonymity or signature validity."""

    model_config = ConfigDict(frozen=True)

    outcome: OoxmlMetadataOutcome
    document_format: Literal["docx", "xlsx", "pptx"] | None = None
    removed_parts: tuple[
        Literal["docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"], ...
    ] = ()
    issues: tuple[OoxmlMetadataIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.removed_parts)) != len(self.removed_parts):
            raise ValueError("removed_parts cannot contain duplicates")
        if self.outcome is OoxmlMetadataOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.document_format is None or self.issues:
            raise ValueError("success requires document format and no issues")
        if self.outcome is OoxmlMetadataOutcome.CHANGED:
            if not self.removed_parts:
                raise ValueError("changed requires removed parts")
        elif self.removed_parts:
            raise ValueError("only changed may report completed removals")
        return self
