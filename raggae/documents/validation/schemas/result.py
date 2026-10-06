from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from collections.abc import Mapping
from enum import StrEnum
from dataclasses import dataclass, field
from typing import Self, TypeAlias
from types import MappingProxyType

FactValue: TypeAlias = str | int | float | bool | None


class Finding(BaseModel):
    """
    One thing a check noticed.

    Carries no severity. Workers report what they found; whether it warns or
    rejects is the coordinator's decision, made from the code.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(
        min_length=1, description="Qualified code, e.g. filename.too_long."
    )
    message: str = Field(min_length=1, description="Human-readable explanation.")
    detail: Mapping[str, FactValue] = Field(
        default_factory=dict,
        validate_default=True,
        description="Machine-readable facts behind the message, such as limit and observed.",
    )

    @field_validator("detail", mode="after")
    @classmethod
    def _freeze(cls, value: Mapping[str, FactValue]) -> Mapping[str, FactValue]:
        return MappingProxyType(dict(value))

    @field_serializer("detail")
    def _serialize(self, value: Mapping[str, FactValue]) -> dict[str, FactValue]:
        return dict(value)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class Severity(StrEnum):
    """
    How much a finding costs.

    Lives beside :class:`Finding` rather than with the policy that applies it,
    because each check ships a default weight for its own codes — the person who
    wrote the check knows best that a dangerous extension is a rejection. What a
    finding *costs at runtime* is still the policy's decision, and it may replace
    every default.
    """

    INFO = "info"
    WARNING = "warning"
    REJECT = "reject"


class WeightedFinding(BaseModel):
    """A finding with the weight the policy gave it."""

    model_config = ConfigDict(frozen=True)

    finding: Finding
    severity: Severity


class CheckOutcome(BaseModel):
    """What a check returns: what it found, and what it established."""

    model_config = ConfigDict(frozen=True)

    findings: tuple[Finding, ...] = Field(
        default=(),
        description="Findings, in the order the check produced them.",
    )

    facts: Mapping[str, FactValue] = Field(
        default_factory=dict,
        validate_default=True,
        description="What the check determined about the document.",
    )

    @field_validator("facts", mode="after")
    @classmethod
    def _freeze(cls, value: Mapping[str, FactValue]) -> Mapping[str, FactValue]:
        return MappingProxyType(dict(value))

    @field_serializer("facts")
    def _serialize(self, value: Mapping[str, FactValue]) -> dict[str, FactValue]:
        return dict(value)

    @property
    def codes(self) -> tuple[str, ...]:
        """The codes found, in order."""
        return tuple(finding.code for finding in self.findings)

    def found(self, code: str) -> bool:
        """Whether *code* was among the findings."""
        return code in self.codes


class CheckRun(BaseModel):
    """
    What one check did, including doing nothing.

    Recorded even when a check was skipped or was a null object: a caller who
    only learns "accepted" cannot otherwise tell a clean document from one that
    nothing looked at.
    """

    model_config = ConfigDict(frozen=True)

    check: str = Field(description="Name of the worker, or of the step it stands for.")
    ran: bool = Field(description="Whether it inspected the document at all.")
    skipped_because: str | None = Field(
        default=None,
        description="Why it did not run — not applicable to this format, or switched off.",
    )

    @model_validator(mode="after")
    def _check_skip_reason(self) -> Self:
        if self.ran and self.skipped_because is not None:
            raise ValueError("A check that ran cannot have a skip reason.")
        return self


@dataclass
class ValidationRun:
    """
    What has accumulated so far in one call.

    Local to a single ``validate``, so a coordinator can be shared and used
    concurrently without one document's findings reaching another's report.
    """

    findings: list[WeightedFinding] = field(default_factory=list)
    facts: dict[str, FactValue] = field(default_factory=dict)
    checks: list[CheckRun] = field(default_factory=list)
    stopped: bool = False
    pdf_access_blocked: bool = False
