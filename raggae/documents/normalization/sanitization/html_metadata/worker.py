"""Remove selected HTML meta tags without rewriting the rest of the document."""

from typing import BinaryIO, Unpack, overload

from .parser import MetadataParser
from .models import (
    HtmlMetadataFinding,
    HtmlMetadataIssue,
    HtmlMetadataOutcome,
    HtmlMetadataReport,
)
from ...io import SharedInputOutputHandler
from ...utils import write_all_bytes
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


class HtmlMetadataSanitizer:
    """Opt-in authoring/description meta-tag removal, not active-content cleanup."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_html_metadata"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[HtmlMetadataReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[HtmlMetadataReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[HtmlMetadataReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> HtmlMetadataReport:
        try:
            text = source.read().decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._failure(
                HtmlMetadataFinding.DECODE_FAILED, "HTML source must be valid UTF-8."
            )
        parser = MetadataParser(text)
        try:
            parser.feed(text)
            parser.close()
        except (ValueError, AssertionError):
            return self._failure(
                HtmlMetadataFinding.SANITIZATION_FAILED,
                "HTML metadata could not be removed.",
            )
        if not parser.removals:
            return HtmlMetadataReport(outcome=HtmlMetadataOutcome.NO_CHANGE)
        # Remove only the recognized start-tag spans. Rebuilding HTML would
        # unnecessarily change unrelated attributes, entities, and formatting.
        pieces = []
        offset = 0
        for start, end in parser.removals:
            pieces.append(text[offset:start])
            offset = end
        pieces.append(text[offset:])
        try:
            data = "".join(pieces).encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return self._failure(
                HtmlMetadataFinding.ENCODE_FAILED,
                "Sanitized HTML could not be encoded.",
            )
        write_all_bytes(output, data)
        return HtmlMetadataReport(
            outcome=HtmlMetadataOutcome.CHANGED,
            removed_tags=len(parser.removals),
        )

    @staticmethod
    def _failure(code: HtmlMetadataFinding, message: str) -> HtmlMetadataReport:
        return HtmlMetadataReport(
            outcome=HtmlMetadataOutcome.FAILED,
            issues=(HtmlMetadataIssue(code=code, message=message),),
        )
