"""Resolve public call arguments through the existing request model."""

from pathlib import Path
from typing import NotRequired, TypedDict, cast

from .artifacts import Artifact
from .models import NormalizationRequest, TextEncoding


class NormalizationArguments(TypedDict):
    """Keyword field types for editors; defaults and validation stay on the request."""

    source: Path | str | bytes | Artifact
    output_path: Path | str | None
    declared_encoding: NotRequired[TextEncoding | None]
    target_encoding: NotRequired[TextEncoding]
    password: NotRequired[str | bytes | None]
    source_format: NotRequired[str | None]


def resolve_request(
    request: NormalizationRequest | None,
    arguments: dict[str, object],
) -> NormalizationRequest:
    """Use a request unchanged, or construct one; never merge the two styles."""
    if request is not None:
        if not isinstance(request, NormalizationRequest):
            raise TypeError("request must be a NormalizationRequest")
        if arguments:
            raise TypeError("pass either a NormalizationRequest or keyword arguments, not both")
        return request
    if not arguments:
        raise TypeError("pass a NormalizationRequest or source/output_path keyword arguments")
    # This is a typing-only description of the dictionary. The constructor
    # still rejects missing fields, unknown keywords, and invalid field values.
    return NormalizationRequest(**cast(NormalizationArguments, arguments))
