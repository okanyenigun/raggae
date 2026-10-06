from pydantic import BaseModel, ConfigDict, Field
from enum import StrEnum
from .result import Severity, Finding
from ..checks.filename.policy import FilenamePolicy
from ..checks.resource_limits.policy import (
    PdfLimits,
    ArchiveLimits,
    ImageLimits,
    TextLimits,
)
from ..checks.active_content.policy import ActiveContentPolicy
from ..checks.capability.policy import CapabilityPolicy
from ..checks.content_type.policy import ContentTypePolicy
from ..checks.encryption.policy import EncryptionPolicy
from ..checks.identity.policy import IdentityPolicy


class Decision(StrEnum):
    """The single answer a validation run produces."""

    ACCEPT = "accept"
    ACCEPT_WITH_WARNINGS = "accept_with_warnings"
    NEEDS_PASSWORD = "needs_password"
    REJECT = "reject"


def assemble_default_severities() -> dict[str, Severity]:
    policies = (
        FilenamePolicy(),
        IdentityPolicy(),
        ContentTypePolicy(),
        EncryptionPolicy(),
        PdfLimits(),
        ArchiveLimits(),
        ImageLimits(),
        TextLimits(),
        ActiveContentPolicy(),
        CapabilityPolicy(),
    )

    assembled: dict[str, Severity] = {}

    for policy in policies:
        for code, severity in policy.severities.items():
            existing = assembled.get(code)

            if existing is not None and existing != severity:
                raise ValueError(
                    f"Conflicting severities for {code!r}: {existing} and {severity}"
                )

            assembled[code] = severity

    return assembled


class DecisionPolicy(BaseModel):
    """
    What each finding costs, and when to stop looking.

    The one place a pipeline states its own judgment. The checks deliberately
    report without weight, because the same finding means different things to
    different pipelines: a name carrying directories may be routine in an archive
    migration and unacceptable at a public upload endpoint.
    """

    model_config = ConfigDict(frozen=True)

    severity_by_code: dict[str, Severity] = Field(
        default_factory=assemble_default_severities,
        description=(
            "What each finding code costs. Defaults to the shipped map; pass your "
            "own to override it entirely, or copy and adjust it."
        ),
    )

    default_severity: Severity = Field(
        default=Severity.WARNING,
        description=(
            "What an unrecognized code costs. Codes arrive faster than policies "
            "are updated, and a new finding must not silently mean nothing."
        ),
    )

    stop_on_first_rejection: bool = Field(
        default=True,
        description=(
            "Whether later checks run once one has rejected. False costs time and "
            "gives the caller every reason at once."
        ),
    )

    recoverable_codes: frozenset[str] = Field(
        default=EncryptionPolicy().recoverable,
        description=(
            "Codes the caller can fix by supplying something. A run rejected only "
            "by these asks for a password rather than refusing."
        ),
    )

    def severity_of(self, finding: Finding) -> Severity:
        """
        The weight this policy gives *finding*.

        An unmapped code falls to :attr:`default_severity` rather than to nothing:
        codes arrive faster than policies are updated, and a finding nobody has
        classified yet must not silently mean it is fine.
        """
        return self.severity_by_code.get(finding.code, self.default_severity)

    def rejects(self, finding: Finding) -> bool:
        """Whether this finding alone stops the document."""
        return self.severity_of(finding) is Severity.REJECT

    def decide(self, findings: tuple[Finding, ...]) -> Decision:
        """
        The single answer these findings add up to.

        A run stopped only by recoverable findings asks for a password instead of
        refusing — but one that also failed for another reason refuses, because
        supplying a password would not change that.
        """
        rejecting = [finding for finding in findings if self.rejects(finding)]
        if rejecting:
            if all(finding.code in self.recoverable_codes for finding in rejecting):
                return Decision.NEEDS_PASSWORD
            return Decision.REJECT
        if any(self.severity_of(finding) is Severity.WARNING for finding in findings):
            return Decision.ACCEPT_WITH_WARNINGS
        return Decision.ACCEPT
