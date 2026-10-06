from pathlib import Path
from .policy import IdentityPolicy
from ...schemas.result import CheckOutcome


class NoOpIdentityProbe:
    """Disables file-identity inspection without filesystem access."""

    def __init__(self, policy: IdentityPolicy | None = None) -> None:
        self._policy = policy or IdentityPolicy()

    def check(self, path: Path) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_identity_noop"

    @property
    def disabled(self) -> bool:
        return True
