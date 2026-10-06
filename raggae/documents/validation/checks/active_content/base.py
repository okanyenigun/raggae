from pathlib import Path
from typing import Protocol, runtime_checkable
from .policy import ActiveContentPolicy, ActiveContentFinding
from ..utils import make_finding
from ...schemas.result import CheckOutcome, Finding


@runtime_checkable
class ActiveContentProbe(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def handles(self) -> frozenset[str]: ...

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome: ...


def reference_finding(
    references: list[str], policy: ActiveContentPolicy, reference_sampled: int = 5
) -> list[Finding]:
    """One finding for every outbound reference the policy does not already allow."""
    reportable = sorted({ref for ref in references if not policy.allows(ref)})
    if not reportable:
        return []
    return [
        make_finding(
            ActiveContentFinding.REMOTE_REFERENCE,
            "Document refers to resources outside itself.",
            count=len(reportable),
            sample=", ".join(reportable[:reference_sampled]),
        )
    ]
