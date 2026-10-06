from typing import Protocol, runtime_checkable
from ...schemas.result import CheckOutcome


@runtime_checkable
class FilenameValidator(Protocol):
    """Checks a submitted filename. Pure string work — no filesystem access."""

    @property
    def name(self) -> str: ...

    def check(self, filename: str) -> CheckOutcome: ...
