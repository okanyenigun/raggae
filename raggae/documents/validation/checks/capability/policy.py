from pydantic import BaseModel, ConfigDict, Field
from enum import StrEnum
from ...schemas.result import Severity


class CapabilityFinding(StrEnum):
    """What the capability probe can report."""

    NOT_APPLICABLE = "capability.not_applicable"
    UNREADABLE = "capability.unreadable"
    EMPTY_DOCUMENT = "capability.empty_document"


class CapabilityPolicy(BaseModel):
    """How hard to look before answering whether a document holds text."""

    model_config = ConfigDict(frozen=True)

    sampled_pages: int = Field(
        default=3,
        gt=0,
        description=(
            "Pages examined before answering. Extracting text from every page is "
            "stage 3's work, and doing it here would double it."
        ),
    )

    min_characters_per_page: int = Field(
        default=20,
        ge=0,
        description=(
            "Characters below which a page counts as having none. A scanned page "
            "is rarely empty — a stray header or page number survives — and one "
            "character is not a text layer."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            CapabilityFinding.EMPTY_DOCUMENT: Severity.WARNING,
            CapabilityFinding.UNREADABLE: Severity.WARNING,
            CapabilityFinding.NOT_APPLICABLE: Severity.INFO,
        },
        description="Default decision severity for each capability finding.",
    )
