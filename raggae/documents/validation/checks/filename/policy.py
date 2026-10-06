from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from enum import StrEnum
from ...schemas.result import Severity
from ...shared import SUPPORTED_EXTENSIONS, DANGEROUS_EXTENSIONS


class FilenameFinding(StrEnum):
    """What filename validation can report."""

    EMPTY = "filename.empty"
    INPUT_TOO_LONG = "filename.input_too_long"
    ENCODED_SEPARATOR = "filename.encoded_separator"
    CONTROL_CHARACTER = "filename.control_character"
    HIDDEN_CHARACTER = "filename.hidden_character"
    PATH_COMPONENTS = "filename.path_components"
    INVALID_BASENAME = "filename.invalid_basename"
    NAME_TOO_LONG = "filename.name_too_long"
    NAME_TOO_MANY_BYTES = "filename.name_too_many_bytes"
    EXTENSION_MISSING = "filename.extension_missing"
    EXTENSION_NOT_ALLOWED = "filename.extension_not_allowed"
    DANGEROUS_EXTENSION = "filename.dangerous_extension"
    NORMALIZED = "filename.normalized"
    WHITESPACE_TRIMMED = "filename.whitespace_trimmed"


class FilenamePolicy(BaseModel):
    """Limits and allow-lists for filename validation."""

    model_config = ConfigDict(frozen=True)

    max_submitted_chars: int = Field(
        default=1_024,
        ge=1,
        description="Length limit on the raw input, before any processing.",
    )

    max_name_chars: int = Field(
        default=200,
        ge=1,
        description="Length limit on the canonical basename, in characters.",
    )

    max_name_utf8_bytes: int = Field(
        default=512,
        ge=1,
        description="Length limit on the canonical basename, in UTF-8 bytes.",
    )

    require_extension: bool = Field(
        default=True,
        description="Whether a name with no extension is rejected.",
    )

    reject_path_components: bool = Field(
        default=True,
        description="Whether a name carrying directories is rejected or just stripped.",
    )

    allowed_extensions: frozenset[str] = Field(
        default=frozenset(SUPPORTED_EXTENSIONS),
        description="Extensions the name may end in.",
    )

    dangerous_extensions: frozenset[str] = Field(
        default=DANGEROUS_EXTENSIONS,
        description="Extensions refused in any suffix position.",
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            FilenameFinding.EMPTY: Severity.REJECT,
            FilenameFinding.INPUT_TOO_LONG: Severity.REJECT,
            FilenameFinding.ENCODED_SEPARATOR: Severity.REJECT,
            FilenameFinding.CONTROL_CHARACTER: Severity.REJECT,
            FilenameFinding.HIDDEN_CHARACTER: Severity.REJECT,
            FilenameFinding.INVALID_BASENAME: Severity.REJECT,
            FilenameFinding.NAME_TOO_LONG: Severity.REJECT,
            FilenameFinding.NAME_TOO_MANY_BYTES: Severity.REJECT,
            FilenameFinding.EXTENSION_NOT_ALLOWED: Severity.REJECT,
            FilenameFinding.DANGEROUS_EXTENSION: Severity.REJECT,
            # Directories were stripped, so the danger is already gone — and some
            # browsers still send a full path for an ordinary upload.
            FilenameFinding.PATH_COMPONENTS: Severity.WARNING,
            # Content-type detection can identify the file without a claim.
            FilenameFinding.EXTENSION_MISSING: Severity.WARNING,
            FilenameFinding.NORMALIZED: Severity.INFO,
            FilenameFinding.WHITESPACE_TRIMMED: Severity.INFO,
        },
        description="Default decision severity for each filename finding.",
    )

    @field_validator("allowed_extensions", "dangerous_extensions", mode="after")
    @classmethod
    def _normalize(cls, value: frozenset[str]) -> frozenset[str]:
        # Suffixes are compared without a dot and case-folded, so {".PDF"} and
        # {"pdf"} have to mean the same thing.
        return frozenset(
            item.strip().lstrip(".").casefold() for item in value if item.strip()
        )

    @model_validator(mode="after")
    def _no_contradiction(self) -> "FilenamePolicy":
        overlap = self.allowed_extensions & self.dangerous_extensions
        if overlap:
            raise ValueError(
                "Extensions cannot be both allowed and dangerous: "
                + ", ".join(sorted(overlap))
            )
        return self
