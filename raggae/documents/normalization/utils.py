"""Shared helpers for normalization inputs and outputs."""

import posixpath
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit


def require_path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError("path must be a Path or str")
    if isinstance(value, str) and not value.strip():
        raise ValueError("path cannot be blank")
    return Path(value).absolute()


def write_all_bytes(
    output: BinaryIO,
    data: bytes,
    *,
    error_message: str = "output did not accept a valid number of bytes",
) -> None:
    """Finish partial writes, rejecting invalid byte counts without hiding errors."""
    offset = 0
    while offset < len(data):
        written = output.write(data[offset:])
        if (
            not isinstance(written, int)
            or isinstance(written, bool)
            or not 0 < written <= len(data) - offset
        ):
            raise OSError(error_message)
        offset += written


def resolve_ooxml_target(relationship_part: str, target: str) -> str:
    """Resolve an internal relationship target to its ZIP-package part name."""
    path = unquote(urlsplit(target.replace("\\", "/")).path)
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    # word/_rels/document.xml.rels resolves from word/document.xml's directory.
    parent = posixpath.dirname(posixpath.dirname(relationship_part))
    return posixpath.normpath(posixpath.join(parent, path))


def ooxml_relationship_part(part: str) -> str:
    """Return the name of the relationship file belonging to a package part."""
    parent, filename = posixpath.split(part)
    return posixpath.join(parent, "_rels", filename + ".rels")


def require_executable(value: object) -> str | None:
    """Validate an optional converter executable without locating or running it."""
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise TypeError("executable must be a string, Path, or None")
    if not str(value).strip():
        raise ValueError("executable cannot be blank")
    return str(value)


def require_timeout_seconds(value: object) -> int:
    """Require the positive integer timeout shared by the native converters."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("timeout_seconds must be an integer")
    if value <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    return value
