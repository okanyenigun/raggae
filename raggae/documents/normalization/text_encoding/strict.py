"""Strict incremental text conversion to a requested encoding, defaulting to UTF-8."""

import codecs
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from typing import BinaryIO, Unpack, overload
from .errors import InvalidTargetEncoding, InvalidTextEncoding
from .models import (
    TextEncodingDetailValue,
    TextEncodingFinding,
    TextEncodingIssue,
    TextEncodingOutcome,
    TextEncodingReport,
)
from ..io import SharedInputOutputHandler
from ..utils import write_all_bytes
from ..models import NormalizationRequest, NormalizationResult, TextEncoding
from ..request import NormalizationArguments, resolve_request


@dataclass(frozen=True, slots=True)
class _EncodingChoice:
    codec: str
    reported_name: str
    bom: bytes = b""


# UTF-32 LE starts with the UTF-16 LE marker: match the longest BOM first.
_BOM_ENCODINGS = (
    _EncodingChoice("utf-32-le", "utf-32-le", codecs.BOM_UTF32_LE),
    _EncodingChoice("utf-32-be", "utf-32-be", codecs.BOM_UTF32_BE),
    _EncodingChoice("utf-8", "utf-8-sig", codecs.BOM_UTF8),
    _EncodingChoice("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE),
    _EncodingChoice("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE),
)


class StrictTextEncodingNormalizer:
    """Preserve characters and newlines; never guess or replace invalid text.

    A Unicode BOM identifies the source. Without one, valid UTF-8 takes
    precedence; other bytes require an explicit Python text-encoding name. The
    public method delegates opening, closing, cleanup, and publication to the
    shared input/output handler. The encoding transformation remains stream-only.
    """

    def __init__(self, *, chunk_size: int = 64 * 1024) -> None:
        if (
            not isinstance(chunk_size, int)
            or isinstance(chunk_size, bool)
            or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer")
        self._chunk_size = chunk_size
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_strict_text_encoding"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[TextEncodingReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[TextEncodingReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[TextEncodingReport]:
        """Accept a request or its keyword fields, without caller-managed streams."""
        request = resolve_request(request, kwargs)

        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=partial(
                self._transform,
                declared_encoding=request.declared_encoding,
                target_encoding=request.target_encoding,
            ),
        )
        return NormalizationResult(report=report, artifact=artifact)

    def _transform(
        self,
        source: BinaryIO,
        output: BinaryIO,
        *,
        declared_encoding: TextEncoding | None = None,
        target_encoding: TextEncoding = "utf-8",
    ) -> TextEncodingReport:
        """Write the requested encoding if changed; discard failed output.

        The I/O handler supplies open, distinct binary streams, and the request
        supplies validated configuration. Source is consumed from its current
        position and may be rewound there for a declared-encoding retry.
        Output is never reset, truncated,
        or closed. Stream I/O errors propagate.
        """

        reader, writer = source, output
        # The request has already restricted this to supported text encodings.
        target = codecs.lookup(target_encoding).name

        starting_position = reader.tell()
        prefix = self._read_prefix(reader)
        choice = next(
            (item for item in _BOM_ENCODINGS if prefix.startswith(item.bom)), None
        )

        if choice is None:
            if self._is_utf8(reader, prefix):
                if target == "utf-8":
                    return TextEncodingReport(
                        outcome=TextEncodingOutcome.NO_CHANGE,
                        source_encoding="utf-8",
                        target_encoding=target,
                    )
                choice = _EncodingChoice("utf-8", "utf-8")
            elif declared_encoding is None:
                return self._failure(
                    TextEncodingFinding.ENCODING_REQUIRED,
                    "Input is not valid UTF-8 and no source encoding was declared.",
                    target_encoding=target,
                )
            else:
                codec = codecs.lookup(declared_encoding)
                choice = _EncodingChoice(codec.name, codec.name)
            reader.seek(starting_position)
            prefix = self._read_prefix(reader)

        try:
            if self._matches_target(choice, target):
                self._validate_source(reader, choice, prefix)
                return TextEncodingReport(
                    outcome=TextEncodingOutcome.NO_CHANGE,
                    source_encoding=choice.reported_name,
                    target_encoding=target,
                )
            self._transcode(reader, writer, choice, prefix, target)
        except InvalidTextEncoding:
            return self._failure(
                TextEncodingFinding.DECODE_FAILED,
                "Input cannot be strictly decoded using the selected source encoding.",
                target_encoding=target,
                source_encoding=choice.reported_name,
            )
        except InvalidTargetEncoding:
            return self._failure(
                TextEncodingFinding.ENCODE_FAILED,
                "Text cannot be strictly represented using the target encoding.",
                target_encoding=target,
                source_encoding=choice.reported_name,
            )
        return TextEncodingReport(
            outcome=TextEncodingOutcome.CHANGED,
            source_encoding=choice.reported_name,
            target_encoding=target,
            bom_removed=bool(choice.bom),
        )

    def _is_utf8(self, reader: BinaryIO, prefix: bytes) -> bool:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            self._decode(decoder, prefix)
        except InvalidTextEncoding:
            return False
        for chunk in self._chunks(reader):
            try:
                self._decode(decoder, chunk)
            except InvalidTextEncoding:
                return False
        try:
            self._decode(decoder, b"", final=True)
        except InvalidTextEncoding:
            return False
        return True

    def _transcode(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        choice: _EncodingChoice,
        prefix: bytes,
        target_encoding: str,
    ) -> None:
        decoder = codecs.getincrementaldecoder(choice.codec)(errors="strict")
        encoder = codecs.getincrementalencoder(target_encoding)(errors="strict")
        self._write_decoded(writer, decoder, encoder, prefix[len(choice.bom) :])
        for chunk in self._chunks(reader):
            self._write_decoded(writer, decoder, encoder, chunk)
        self._write_decoded(writer, decoder, encoder, b"", final=True)

    def _validate_source(
        self, reader: BinaryIO, choice: _EncodingChoice, prefix: bytes
    ) -> None:
        decoder = codecs.getincrementaldecoder(choice.codec)(errors="strict")
        self._decode(decoder, prefix[len(choice.bom) :])
        for chunk in self._chunks(reader):
            self._decode(decoder, chunk)
        self._decode(decoder, b"", final=True)

    @staticmethod
    def _matches_target(choice: _EncodingChoice, target: str) -> bool:
        if not choice.bom:
            return choice.codec == target
        # Preserve a source marker only when the target would emit the same BOM.
        # UTF-16/32 targets use native byte order.
        target_bom = {
            "utf-8-sig": codecs.BOM_UTF8,
            "utf-16": codecs.BOM_UTF16,
            "utf-32": codecs.BOM_UTF32,
        }.get(target)
        return target_bom is not None and choice.bom == target_bom

    @staticmethod
    def _decode(
        decoder: codecs.IncrementalDecoder, content: bytes, *, final: bool = False
    ) -> str:
        try:
            return decoder.decode(content, final=final)
        except UnicodeError as error:
            raise InvalidTextEncoding from error

    @classmethod
    def _write_decoded(
        cls,
        writer: BinaryIO,
        decoder: codecs.IncrementalDecoder,
        encoder: codecs.IncrementalEncoder,
        content: bytes,
        *,
        final: bool = False,
    ) -> None:
        text = cls._decode(decoder, content, final=final)
        try:
            encoded = encoder.encode(text, final=final)
        except UnicodeError as error:
            raise InvalidTargetEncoding from error
        # Keep destination exceptions outside the encoding-error boundary.
        write_all_bytes(
            writer, encoded, error_message="output stream did not return a valid byte count"
        )

    def _read_prefix(self, reader: BinaryIO) -> bytes:
        prefix = bytearray()
        while len(prefix) < 4:
            chunk = reader.read(min(4 - len(prefix), self._chunk_size))
            if not chunk:
                break
            prefix.extend(chunk)
        return bytes(prefix)

    def _chunks(self, reader: BinaryIO) -> Iterator[bytes]:
        while chunk := reader.read(self._chunk_size):
            yield chunk

    @staticmethod
    def _failure(
        code: TextEncodingFinding,
        message: str,
        *,
        target_encoding: str = "utf-8",
        **detail: TextEncodingDetailValue,
    ) -> TextEncodingReport:
        return TextEncodingReport(
            outcome=TextEncodingOutcome.FAILED,
            target_encoding=target_encoding,
            issues=(TextEncodingIssue(code=code, message=message, detail=detail),),
        )
