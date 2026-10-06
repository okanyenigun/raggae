from pathlib import Path
from .policy import ContentTypePolicy
from ...schemas.result import CheckOutcome


class NoOpContentTypeDetector:
    """Disables content-type detection without reading the file."""

    def __init__(self, policy: ContentTypePolicy | None = None) -> None:
        self._policy = policy or ContentTypePolicy()

    def check(self, path: Path, claimed_extension: str | None = None) -> CheckOutcome:
        return CheckOutcome()

    @property
    def name(self) -> str:
        return "validation_content_type_noop"

    @property
    def disabled(self) -> bool:
        return True
