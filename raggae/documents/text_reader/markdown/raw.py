"""Read literal Markdown source without parsing, rendering, or rewriting it."""

from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
from typing import get_args
from ..utils.sources import prepare_source
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)

from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class RawMarkdownTextReader:
    """Return the complete decoded Markdown source as one Markdown block.

    Preserve markup, whitespace, line endings, raw HTML, and link destinations.
    Nothing is parsed, rendered, fetched, executed, or sanitized. This worker
    requires no optional parser and cannot honor layout-region requests.
    """

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "text_reader_raw_markdown"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        prepared = prepare_source(
            source,
            reader=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message="RawMarkdownTextReader reads whole sources, not layout regions.",
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        source = prepared

        try:
            if source.path is not None and not S_ISREG(source.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = source.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )

        if not text:
            return TextReadResult(
                reader=self.name,
                outcome="success",
                findings=(
                    TextReadFinding(
                        code=FindingCode.EMPTY,
                        message="Source contains no decoded characters.",
                        page_index=0,
                    ),
                ),
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=(
                TextBlock(
                    text=text, page_index=0, content_format=TextContentFormat.MARKDOWN
                ),
            ),
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return TextReadResult(
            reader=self.name,
            outcome="failed",
            findings=(TextReadFinding(code=code, message=message),),
        )
