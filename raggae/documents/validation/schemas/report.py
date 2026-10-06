from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from collections.abc import Mapping
from types import MappingProxyType
from .decision import Decision
from .result import CheckRun, FactValue, Severity, WeightedFinding


class DocumentValidationReport(BaseModel):
    """The decision, findings, facts, and audit trail from one validation run."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    findings: tuple[WeightedFinding, ...] = ()
    facts: Mapping[str, FactValue] = Field(
        default_factory=dict,
        validate_default=True,
        description="Read-only snapshot of the facts established during validation.",
    )
    checks: tuple[CheckRun, ...] = ()

    @field_validator("facts", mode="after")
    @classmethod
    def _freeze(cls, value: Mapping[str, FactValue]) -> Mapping[str, FactValue]:
        return MappingProxyType(dict(value))

    @field_serializer("facts")
    def _serialize(self, value: Mapping[str, FactValue]) -> dict[str, FactValue]:
        return dict(value)

    @property
    def accepted(self) -> bool:
        return self.decision in {Decision.ACCEPT, Decision.ACCEPT_WITH_WARNINGS}

    @property
    def rejecting_findings(self) -> tuple[WeightedFinding, ...]:
        return tuple(
            weighted
            for weighted in self.findings
            if weighted.severity is Severity.REJECT
        )

    @property
    def checks_that_ran(self) -> tuple[str, ...]:
        return tuple(check.check for check in self.checks if check.ran)

    @property
    def checks_that_did_not(self) -> tuple[str, ...]:
        return tuple(check.check for check in self.checks if not check.ran)
