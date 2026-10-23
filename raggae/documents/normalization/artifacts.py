"""A document represented by a file path or immutable bytes."""

from dataclasses import dataclass, field
from pathlib import Path
from .utils import require_path


@dataclass(frozen=True, slots=True, init=False)
class Artifact:
    """Only the document's location or bytes; no ownership or lifetime system."""

    path: Path | None
    data: bytes | None = field(repr=False)

    def __init__(
        self, *, path: Path | str | None = None, data: bytes | None = None
    ) -> None:
        if (path is None) == (data is None):
            raise ValueError("artifact requires exactly one of path or data")

        if data is not None and not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")

        object.__setattr__(self, "path", None if path is None else require_path(path))
        object.__setattr__(self, "data", data)

    def read_bytes(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        assert self.data is not None
        return self.data
