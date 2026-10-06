from pathlib import Path
from ...schemas.result import CheckOutcome


class NoOpResourceProbe:
    """Disables resource inspection and advertises no format coverage."""

    def __init__(self, policy=None) -> None:
        self._policy = policy

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_resource_noop"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset()

    @property
    def disabled(self) -> bool:
        return True
