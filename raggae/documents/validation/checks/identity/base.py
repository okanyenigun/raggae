from typing import Protocol, runtime_checkable
from pathlib import Path
from ...schemas.result import CheckOutcome


@runtime_checkable
class IdentityProbe(Protocol):
    """
    Confirms the path is a readable regular file within the size limit, and
    fingerprints it.
    """

    @property
    def name(self) -> str: ...

    def check(self, path: Path) -> CheckOutcome: ...
