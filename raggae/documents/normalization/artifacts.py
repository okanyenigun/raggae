"""A document's path or bytes, plus optional access information."""

from dataclasses import dataclass, field
from pathlib import Path
from .utils import require_path


@dataclass(frozen=True, slots=True, init=False)
class Artifact:
    """A document copy and its optional password; no ownership/lifetime system."""

    path: Path | None
    data: bytes | None = field(repr=False)
    password: str | None = field(default=None, repr=False)

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        data: bytes | None = None,
        password: str | None = None,
    ) -> None:
        if (path is None) == (data is None):
            raise ValueError("artifact requires exactly one of path or data")

        if data is not None and not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        if password is not None and not isinstance(password, str):
            raise TypeError("password must be str or None")

        object.__setattr__(self, "path", None if path is None else require_path(path))
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "password", password)

    def read_bytes(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        assert self.data is not None
        return self.data
