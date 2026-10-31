"""One request and document-result contract for normalization workers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args
from pydantic import BaseModel
from .artifacts import Artifact
from .utils import require_path


TextEncoding = Literal[
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-32",
    "cp1252",
    "iso-8859-1",
    "ascii",
]


@dataclass(frozen=True, slots=True, init=False)
class NormalizationRequest:
    """Source plus output path; explicitly use None to keep changed bytes in memory."""

    source: Artifact
    output_path: Path | None
    source_format: str | None
    declared_encoding: TextEncoding | None
    target_encoding: TextEncoding

    def __init__(
        self,
        *,
        source: Path | str | bytes | Artifact,
        output_path: Path | str | None,
        declared_encoding: TextEncoding | None = None,
        target_encoding: TextEncoding = "utf-8",
        source_format: str | None = None,
    ) -> None:
        if isinstance(source, bytes):
            source = Artifact(data=source)
        elif not isinstance(source, Artifact):
            source = Artifact(path=source)
        destination = None if output_path is None else require_path(output_path)
        if declared_encoding is not None:
            if not isinstance(declared_encoding, str):
                raise TypeError("declared_encoding must be a string or None")
            if not declared_encoding.strip():
                raise ValueError("declared_encoding cannot be blank")
            declaration = declared_encoding.strip()
            if declaration not in get_args(TextEncoding):
                raise ValueError(
                    "declared_encoding must be one of: "
                    + ", ".join(get_args(TextEncoding))
                )
            declared_encoding = cast(TextEncoding, declaration)
        if not isinstance(target_encoding, str):
            raise TypeError("target_encoding must be a string")
        if not target_encoding.strip():
            raise ValueError("target_encoding cannot be blank")
        target = target_encoding.strip()
        if target not in get_args(TextEncoding):
            raise ValueError(
                "target_encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        if source_format is not None:
            if not isinstance(source_format, str):
                raise TypeError("source_format must be a string or None")
            source_format = source_format.strip().removeprefix(".").casefold()
            if not source_format:
                raise ValueError("source_format cannot be blank")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "output_path", destination)
        object.__setattr__(self, "source_format", source_format)
        object.__setattr__(self, "declared_encoding", declared_encoding)
        object.__setattr__(self, "target_encoding", target)


@dataclass(frozen=True, slots=True)
class NormalizationResult[ReportT: BaseModel]:
    """The usable document plus the worker-specific serializable report."""

    artifact: Artifact | None
    report: ReportT

    def __post_init__(self) -> None:
        if not isinstance(self.report, BaseModel):
            raise TypeError("report must be a worker report model")
        if self.artifact is not None and not isinstance(self.artifact, Artifact):
            raise TypeError("artifact must be an Artifact or None")
        if self.outcome not in ("changed", "no_change", "failed"):
            raise ValueError("report must provide a supported normalization outcome")
        if self.outcome == "failed":
            if self.artifact is not None:
                raise ValueError("a failed result cannot contain a usable artifact")
        elif self.artifact is None:
            raise ValueError("a successful result requires an artifact")

    @property
    def outcome(self) -> str:
        """Common outcome; all other facts stay on the worker's report."""
        return cast(str, getattr(self.report, "outcome", None))
