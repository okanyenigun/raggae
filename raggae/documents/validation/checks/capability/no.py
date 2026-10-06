from pathlib import Path
from .policy import CapabilityPolicy
from ...schemas.result import CheckOutcome


class NoOpCapabilityProbe:
    """Disables capability inspection and advertises no format coverage."""

    def __init__(self, policy: CapabilityPolicy | None = None) -> None:
        self._policy = policy or CapabilityPolicy()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_capability_noop"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset()

    @property
    def disabled(self) -> bool:
        return True
