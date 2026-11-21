"""Shared JSON decoding, source handling, encoding, and pointer operations."""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from ..models import FindingCode, TextReadResult
from ..utils.sources import failure, read_source_text, require_encoding
from ..utils.sources import prepare_source as _prepare_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


__all__ = [
    "DuplicateJsonKeyError",
    "dump_json",
    "failure",
    "load_document",
    "loads_json",
    "pointer_parts",
    "prepare_source",
    "read_source_text",
    "require_encoding",
    "resolve_pointer",
]


class _JsonNumber(str):
    """Retain the exact numeric token rather than converting to int or float."""


class DuplicateJsonKeyError(ValueError):
    """Do not silently choose a value when an object repeats a key."""


def prepare_source(
    source: Path | str | bytes | Artifact,
    *,
    reader: str,
    layout: LayoutAnalysisResult | None,
    region_ids: Sequence[str] | None,
) -> Artifact | TextReadResult:
    """Resolve input and reject unsupported layout selection before source IO."""
    return _prepare_source(
        source,
        reader=reader,
        layout=layout,
        region_ids=region_ids,
        unsupported_message="This JSON reader reads whole sources, not layout regions.",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constants are not supported.")


def loads_json(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_int=_JsonNumber,
        parse_float=_JsonNumber,
        parse_constant=_reject_constant,
    )


def load_document(
    source: Artifact, *, encoding: TextEncoding, reader: str
) -> tuple[str, Any] | TextReadResult:
    """Decode one complete document, retaining numeric tokens and source order."""
    text = read_source_text(source, encoding=encoding, reader=reader)
    if isinstance(text, TextReadResult):
        return text
    try:
        return text, loads_json(text)
    except DuplicateJsonKeyError:
        return failure(
            reader,
            FindingCode.DUPLICATE_KEY,
            "JSON contains duplicate object keys; no values were returned.",
            page_index=0,
        )
    except (ValueError, RecursionError):
        return failure(
            reader,
            FindingCode.PARSE_FAILED,
            "Source could not be parsed as JSON.",
            page_index=0,
        )


def dump_json(value: Any) -> str:
    """Encode decoded subtrees without rounding numbers or recursive walks."""
    pieces: list[str] = []
    pending: list[tuple[bool, Any]] = [(False, value)]
    while pending:
        literal, current = pending.pop()
        if literal:
            pieces.append(current)
        elif isinstance(current, _JsonNumber):
            pieces.append(str(current))
        elif isinstance(current, dict):
            pieces.append("{")
            pending.append((True, "}"))
            items = list(current.items())
            for index in range(len(items) - 1, -1, -1):
                key, child = items[index]
                pending.append((False, child))
                pending.append((True, json.dumps(key) + ":"))
                if index:
                    pending.append((True, ","))
        elif isinstance(current, list):
            pieces.append("[")
            pending.append((True, "]"))
            for index in range(len(current) - 1, -1, -1):
                pending.append((False, current[index]))
                if index:
                    pending.append((True, ","))
        else:
            pieces.append(json.dumps(current, allow_nan=False))
    return "".join(pieces)


def pointer_parts(pointer: str) -> tuple[str, ...]:
    """Validate a JSON Pointer string, not a URI fragment or wildcard query."""
    if not isinstance(pointer, str):
        raise TypeError("pointer must be a string")
    if pointer == "":
        return ()
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError(
            "pointer must be an empty string or a valid slash-prefixed JSON Pointer"
        )
    return tuple(
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    )


def resolve_pointer(value: Any, parts: tuple[str, ...]) -> Any:
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            if len(part) > len(str(len(value))):
                raise LookupError
            index = int(part)
            if index >= len(value):
                raise LookupError
            value = value[index]
        else:
            raise LookupError
    return value
