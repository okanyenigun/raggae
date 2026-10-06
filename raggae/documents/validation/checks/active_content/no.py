from pathlib import Path
from .policy import ActiveContentPolicy
from ...schemas.result import CheckOutcome


class NoOpActiveContentProbe:
    """Disables active-content inspection and advertises no format coverage."""

    def __init__(self, policy: ActiveContentPolicy | None = None) -> None:
        self._policy = policy or ActiveContentPolicy()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_active_content_noop"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset()

    @property
    def disabled(self) -> bool:
        return True
