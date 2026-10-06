from typing import Protocol, runtime_checkable
from pathlib import Path
from ...schemas.result import CheckOutcome


@runtime_checkable
class ContentTypeDetector(Protocol):
    """
    Identifies the real format from the file's bytes, and compares it against
    the extension the filename claimed.
    """

    @property
    def name(self) -> str: ...

    def check(
        self, path: Path, claimed_extension: str | None = None
    ) -> CheckOutcome: ...
