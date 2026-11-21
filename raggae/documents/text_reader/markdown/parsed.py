"""Read Markdown prose and code, without rendering HTML or following links."""

from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, get_args
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import prepare_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


if TYPE_CHECKING:
    from markdown_it.token import Token


class MarkdownTextReader:
    """Extract plain text from CommonMark plus tables and strikethrough.

    Paragraphs, headings, list paragraphs, code blocks, and table cells become
    separate blocks. Links contribute labels; images contribute alt text only.
    Raw HTML blocks and paragraphs containing inline HTML are explicitly omitted.
    No HTML rendering, code execution, network access, OCR, or resource policy is
    introduced. This whole-document reader cannot honor layout-region requests.
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
        return "text_reader_markdown"

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
            unsupported_message="MarkdownTextReader reads whole sources, not layout regions.",
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

        try:
            from markdown_it import MarkdownIt
        except ImportError as error:
            raise ImportError(
                "Markdown text reading requires: pip install 'raggae[text-markdown]'"
            ) from error

        blocks: list[TextBlock] = []
        html_omitted = False
        try:
            tokens = (
                MarkdownIt("commonmark").enable(["table", "strikethrough"]).parse(text)
            )
            for token in tokens:
                if token.type == "inline":
                    content = self._inline_text(token.children or [])
                    if content is None:
                        html_omitted = True
                        continue
                elif token.type in ("fence", "code_block"):
                    content = token.content
                elif token.type == "html_block":
                    html_omitted = True
                    continue
                else:
                    continue
                if content:
                    blocks.append(TextBlock(text=content, page_index=0))
        except (ValueError, RecursionError):
            return self._failure(
                FindingCode.PARSE_FAILED, "Markdown could not be parsed."
            )

        findings: tuple[TextReadFinding, ...] = ()
        if html_omitted:
            findings = (
                TextReadFinding(
                    code=FindingCode.HTML_OMITTED,
                    message="Raw HTML blocks and paragraphs containing inline HTML were not read.",
                    page_index=0,
                ),
            )
        elif not blocks:
            findings = (
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="Source contains no extractable Markdown text.",
                    page_index=0,
                ),
            )
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if blocks else "failed") if html_omitted else "success",
            blocks=tuple(blocks),
            findings=findings,
        )

    @staticmethod
    def _inline_text(tokens: Sequence["Token"]) -> str | None:
        """Collect visible inline text; None reports unsupported raw HTML."""
        parts: list[str] = []
        for token in tokens:
            if token.type == "html_inline":
                return None
            if token.type in ("text", "code_inline"):
                parts.append(token.content)
            elif token.type in ("softbreak", "hardbreak"):
                parts.append("\n")
            elif token.children:
                child_text = MarkdownTextReader._inline_text(token.children)
                if child_text is None:
                    return None
                parts.append(child_text)
        return "".join(parts)

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return TextReadResult(
            reader=self.name,
            outcome="failed",
            findings=(TextReadFinding(code=code, message=message),),
        )
