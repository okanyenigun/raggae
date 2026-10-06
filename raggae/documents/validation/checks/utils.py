from contextlib import contextmanager
from collections.abc import Iterator
from PIL import Image
from pathlib import Path
from ..shared import EXTENSION_ALIASES
from ..schemas.result import Finding, FactValue


def require_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise TypeError(f"path must be a Path or str, got {type(value).__name__}")


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lstrip(".").casefold()
    return EXTENSION_ALIASES.get(normalized, normalized)


def make_finding(code: str, message: str, **detail: FactValue) -> Finding:
    return Finding(code=code, message=message, detail=detail)


def applicability(
    handles: frozenset[str],
    detected_format: str | None,
    probe_name: str,
    code: str,
) -> list[Finding]:
    if detected_format is None:
        return []
    fmt = normalize_extension(detected_format)
    if fmt in handles:
        return []
    return [
        make_finding(
            code,
            f"{probe_name} covers {', '.join(sorted(handles))}; this file is {fmt!r}.",
            detected_format=fmt,
        )
    ]


def unreadable(code: str, path: Path, error: BaseException) -> Finding:
    return make_finding(code, f"File could not be read: {error}.", path=path.name)


@contextmanager
def pillow_limit_lifted() -> Iterator[None]:
    """
    Read image headers with Pillow's own bomb ceiling out of the way.

    Pillow raises ``DecompressionBombError`` from ``open()`` past roughly 179M
    pixels, which would make a bomb look like an unreadable file. Our limit is the
    one that should speak, and lifting the ceiling is safe here because opening
    never decodes — only the header is read.
    """
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous
