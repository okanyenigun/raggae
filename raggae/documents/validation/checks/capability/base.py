from typing import Protocol, runtime_checkable
from pathlib import Path
from ...schemas.result import CheckOutcome


@runtime_checkable
class CapabilityProbe(Protocol):
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
