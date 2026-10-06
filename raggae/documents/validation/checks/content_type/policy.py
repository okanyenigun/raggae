from pydantic import BaseModel, ConfigDict, Field, field_validator
from enum import StrEnum
from ...schemas.result import Severity
from ...shared import SUPPORTED_FORMATS


class ContentTypeFinding(StrEnum):
    """What content-type detection can report."""

    UNREADABLE = "content_type.unreadable"
    UNDETERMINED = "content_type.undetermined"
    NOT_ALLOWED = "content_type.not_allowed"
    EXTENSION_MISMATCH = "content_type.extension_mismatch"
    SUBTYPE_ASSUMED = "content_type.subtype_assumed"


class ContentTypePolicy(BaseModel):
    """What content-type detection reads, and which formats it will accept."""

    model_config = ConfigDict(frozen=True)

    prefix_bytes: int = Field(
        default=8_192,
        ge=16,
        description=(
            "How much of the file head is read. The floor fits every signature; "
            "OOXML subtype markers need far more, so shrinking this trades "
            "subtype detection for a smaller read."
        ),
    )

    allowed_formats: frozenset[str] = Field(
        default=frozenset(SUPPORTED_FORMATS),
        description=(
            "Canonical formats the pipeline accepts. Detection recognizes more "
            "than this — a plain ZIP is identifiable and unacceptable."
        ),
    )

    trust_extension_for_text: bool = Field(
        default=True,
        description=(
            "Whether a text file is typed from its claimed extension. Nothing "
            "in the bytes distinguishes txt from md from csv."
        ),
    )

    require_known_format: bool = Field(
        default=True,
        description=(
            "Whether a file that matches no signature is reported. An "
            "unidentifiable file cannot be routed to a branch."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            ContentTypeFinding.UNREADABLE: Severity.REJECT,
            ContentTypeFinding.NOT_ALLOWED: Severity.REJECT,
            # Nothing can be routed, so every later check is skipped. Accepting it
            # would mean accepting a document nothing looked at.
            ContentTypeFinding.UNDETERMINED: Severity.REJECT,
            # The name disagrees with the bytes. If the bytes are a format we refuse,
            # NOT_ALLOWED rejects it anyway; otherwise it is merely mislabelled.
            ContentTypeFinding.EXTENSION_MISMATCH: Severity.WARNING,
            # A note about how we identified it, not about the document.
            ContentTypeFinding.SUBTYPE_ASSUMED: Severity.INFO,
        },
        description="Default decision severity for each content-type finding.",
    )

    @field_validator("allowed_formats", mode="after")
    @classmethod
    def _known_formats(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(
            item.strip().lstrip(".").casefold() for item in value if item.strip()
        )
        unknown = sorted(normalized - SUPPORTED_FORMATS)
        if unknown:
            raise ValueError(
                "Not canonical format names: "
                + ", ".join(unknown)
                + f". Choose from: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        return normalized
