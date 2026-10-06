from .policy import FilenamePolicy
from ...schemas.result import CheckOutcome


class NoOpFilenameValidator:
    """Disables filename validation without inspecting the input."""

    def __init__(self, policy: FilenamePolicy | None = None) -> None:
        self._policy = policy or FilenamePolicy()

    def check(self, filename: str) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_filename_noop"

    @property
    def disabled(self) -> bool:
        return True
