"""Remove selected HTML tags, handlers, and URL attributes, not all browser risks."""

from collections.abc import Callable
from typing import BinaryIO, Unpack, overload
from .rewriter import HtmlRewriter
from .models import (
    HtmlActiveContentFinding,
    HtmlActiveContentIssue,
    HtmlActiveContentOutcome,
    HtmlActiveContentReport,
)
from ...io import SharedInputOutputHandler
from ...utils import write_all_bytes
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


class HtmlActiveContentSanitizer:
    """Opt-in limited rewriting of UTF-8 HTML, with shared resource handling."""

    def __init__(
        self, *, parser_factory: Callable[[], HtmlRewriter] = HtmlRewriter
    ) -> None:
        if not callable(parser_factory):
            raise TypeError("parser_factory must be callable")
        self._parser_factory = parser_factory
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_html_active_content"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[HtmlActiveContentReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[HtmlActiveContentReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[HtmlActiveContentReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> HtmlActiveContentReport:
        try:
            text = source.read().decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._failure(
                HtmlActiveContentFinding.DECODE_FAILED,
                "HTML source must be valid UTF-8.",
            )
        parser = self._parser_factory()
        try:
            parser.feed(text)
            parser.close()
        except ValueError:
            return self._failure(
                HtmlActiveContentFinding.SANITIZATION_FAILED,
                "HTML active content could not be rewritten.",
            )
        if not parser.removals:
            return HtmlActiveContentReport(outcome=HtmlActiveContentOutcome.NO_CHANGE)
        try:
            data = "".join(parser.parts).encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return self._failure(
                HtmlActiveContentFinding.ENCODE_FAILED,
                "Sanitized HTML could not be encoded.",
            )
        # Do not convert destination errors into document findings.
        write_all_bytes(output, data)
        return HtmlActiveContentReport(
            outcome=HtmlActiveContentOutcome.CHANGED,
            removals=dict(parser.removals),
        )

    @staticmethod
    def _failure(
        code: HtmlActiveContentFinding, message: str
    ) -> HtmlActiveContentReport:
        return HtmlActiveContentReport(
            outcome=HtmlActiveContentOutcome.FAILED,
            issues=(HtmlActiveContentIssue(code=code, message=message),),
        )
