"""The worker's encoding, stream, and caller-owned storage contracts."""

import codecs
from io import BytesIO
from pathlib import Path

import pytest

from raggae.documents.normalization import (
    NormalizationRequest,
    StrictTextEncodingNormalizer,
    TextEncodingFinding,
    NormalizationWorker,
    TextEncodingOutcome,
)


_BOMS = (
    ("utf-8", codecs.BOM_UTF8, "utf-8-sig"),
    ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16-le"),
    ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16-be"),
    ("utf-32-le", codecs.BOM_UTF32_LE, "utf-32-le"),
    ("utf-32-be", codecs.BOM_UTF32_BE, "utf-32-be"),
)
_TEXT = 'café € 你好 😀\r\n"a\nb",e\u0301\r\t\n\ufeffend'


class _ShortReader(BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        assert size >= 0, "the worker must never request an unbounded read"
        return super().read(min(size, 1))


class _Writer:
    """A writable binary destination with no seeking or path API."""

    def __init__(self, *, max_write=None):
        self.content = bytearray()
        self.max_write = max_write
        self.calls = 0

    def writable(self):
        return True

    def write(self, content):
        self.calls += 1
        accepted = content if self.max_write is None else content[: self.max_write]
        self.content.extend(accepted)
        return len(accepted)


def test_public_worker_implements_protocol_and_has_name():
    worker = StrictTextEncodingNormalizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_strict_text_encoding"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")


def test_chunk_size_is_not_publicly_exposed():
    assert not hasattr(StrictTextEncodingNormalizer(), "chunk_size")
    assert not hasattr(StrictTextEncodingNormalizer(chunk_size=1), "chunk_size")


@pytest.mark.parametrize("text", ["", "plain ASCII", _TEXT, "already contains �"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 65536])
def test_valid_utf8_writes_nothing_and_keeps_original_bytes(text, chunk_size):
    content = text.encode("utf-8")
    source, output = BytesIO(content), _Writer()
    result = StrictTextEncodingNormalizer(chunk_size=chunk_size)._transform(source, output)
    assert result.outcome is TextEncodingOutcome.NO_CHANGE
    assert result.source_encoding == "utf-8"
    assert result.bom_removed is False
    assert result.issues == ()
    assert output.calls == 0
    assert source.getvalue() == content
    assert not source.closed


@pytest.mark.parametrize("encoding,bom,reported", _BOMS)
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 65536])
def test_unicode_boms_are_removed_without_changing_text(encoding, bom, reported, chunk_size):
    content = bom + _TEXT.encode(encoding)
    source, output = BytesIO(content), BytesIO()
    result = StrictTextEncodingNormalizer(chunk_size=chunk_size)._transform(source, output)
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.source_encoding == reported
    assert result.bom_removed is True
    assert result.issues == ()
    assert output.getvalue() == _TEXT.encode("utf-8")
    assert source.getvalue() == content
    assert not source.closed and not output.closed


@pytest.mark.parametrize("encoding,bom,reported", _BOMS)
def test_bom_only_input_becomes_an_empty_utf8_document(encoding, bom, reported):
    output = BytesIO()
    result = StrictTextEncodingNormalizer()._transform(BytesIO(bom), output)
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.source_encoding == reported
    assert result.bom_removed
    assert output.getvalue() == b""


@pytest.mark.parametrize("encoding,bom,reported", _BOMS)
def test_bom_detection_handles_short_reads_and_respects_chunk_size(encoding, bom, reported):
    source, output = _ShortReader(bom + _TEXT.encode(encoding)), BytesIO()
    result = StrictTextEncodingNormalizer(chunk_size=2)._transform(source, output)
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.source_encoding == reported
    assert output.getvalue() == _TEXT.encode("utf-8")
    assert max(source.requests) <= 2


@pytest.mark.parametrize("encoding,text,reported", [
    ("cp1252", "price: 10€ — café\r\n", "cp1252"),
    ("iso-8859-1", "café\r\n", "iso8859-1"),
])
def test_declared_encoding_is_used_after_utf8_fails(encoding, text, reported):
    source, output = _ShortReader(text.encode(encoding)), _Writer(max_write=1)
    result = StrictTextEncodingNormalizer(chunk_size=3)._transform(
        source, output, declared_encoding=encoding
    )
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.source_encoding == reported
    assert not result.bom_removed
    assert bytes(output.content) == text.encode("utf-8")
    assert max(source.requests) <= 3


@pytest.mark.parametrize("declaration", ["cp1252", "utf-16", "ascii"])
def test_valid_utf8_takes_precedence_over_a_declaration(declaration):
    output = _Writer()
    result = StrictTextEncodingNormalizer()._transform(
        BytesIO("café".encode("utf-8")), output, declared_encoding=declaration
    )
    assert result.outcome is TextEncodingOutcome.NO_CHANGE
    assert output.calls == 0


def test_bom_takes_precedence_over_a_conflicting_declaration():
    output = BytesIO()
    result = StrictTextEncodingNormalizer()._transform(
        BytesIO(codecs.BOM_UTF16_LE + "café".encode("utf-16-le")),
        output,
        declared_encoding="cp1252",
    )
    assert result.source_encoding == "utf-16-le"
    assert output.getvalue() == "café".encode("utf-8")


@pytest.mark.parametrize("content", [b"\xff", b"\xc3", b"10\x80", b"\xc0\xaf"])
def test_non_utf8_without_evidence_is_not_guessed(content):
    output = _Writer()
    result = StrictTextEncodingNormalizer()._transform(BytesIO(content), output)
    assert result.outcome is TextEncodingOutcome.FAILED
    assert result.issues[0].code is TextEncodingFinding.ENCODING_REQUIRED
    assert result.source_encoding is None
    assert output.calls == 0


@pytest.mark.parametrize("declaration", [
    "unknown-encoding", "base64_codec", "hex_codec", "zlib_codec", "rot_13",
])
def test_unknown_and_non_text_source_codecs_are_configuration_errors(declaration):
    with pytest.raises(ValueError, match="declared_encoding must be one of"):
        NormalizationRequest(source=b"\xff", output_path=None, declared_encoding=declaration)


@pytest.mark.parametrize("content", [
    codecs.BOM_UTF8 + b"\xff",
    codecs.BOM_UTF16_LE + b"\x00",
    codecs.BOM_UTF16_BE + b"\xd8\x00",
    codecs.BOM_UTF32_LE + b"\x00",
    codecs.BOM_UTF32_BE + b"\x00\x00\xd8\x00",
])
def test_invalid_or_truncated_bom_selected_text_fails_strictly(content):
    output = BytesIO()
    result = StrictTextEncodingNormalizer(chunk_size=1)._transform(BytesIO(content), output)
    assert result.outcome is TextEncodingOutcome.FAILED
    assert result.issues[0].code is TextEncodingFinding.DECODE_FAILED
    assert not result.bom_removed
    assert "�".encode("utf-8") not in output.getvalue()


def test_incorrect_declaration_fails_instead_of_replacing_characters():
    result = StrictTextEncodingNormalizer()._transform(
        BytesIO(b"caf\xe9"), BytesIO(), declared_encoding="ascii"
    )
    assert result.outcome is TextEncodingOutcome.FAILED
    assert result.issues[0].code is TextEncodingFinding.DECODE_FAILED
    assert result.issues[0].detail == {"source_encoding": "ascii"}


def test_a_late_decode_failure_leaves_partial_output_for_the_caller_to_discard():
    secret = "PRIVATE DOCUMENT TEXT"
    output = BytesIO()
    result = StrictTextEncodingNormalizer(chunk_size=1)._transform(
        BytesIO(codecs.BOM_UTF16_LE + secret.encode("utf-16-le") + b"x"), output
    )
    assert result.outcome is TextEncodingOutcome.FAILED
    assert output.getvalue() == secret.encode("utf-8")
    assert secret not in result.model_dump_json()


def test_worker_processes_from_current_positions_without_resetting_output():
    source = BytesIO(b"ignored:" + b"caf\xe9")
    source.seek(len(b"ignored:"))
    output = BytesIO(b"prefix:")
    output.seek(0, 2)
    result = StrictTextEncodingNormalizer()._transform(
        source, output, declared_encoding="cp1252"
    )
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert output.getvalue() == "prefix:café".encode("utf-8")


def test_no_change_does_not_truncate_or_touch_prepared_output():
    output = BytesIO(b"caller-owned content")
    result = StrictTextEncodingNormalizer()._transform(BytesIO(b"hello"), output)
    assert result.outcome is TextEncodingOutcome.NO_CHANGE
    assert output.getvalue() == b"caller-owned content"
    assert output.tell() == 0


def test_caller_can_choose_a_file_or_memory_without_modifying_original(tmp_path: Path):
    source_path = tmp_path / "source.csv"
    destination_path = tmp_path / "normalized.csv"
    text = "name,city\r\nJosé,İstanbul\r\n"
    original = codecs.BOM_UTF16_LE + text.encode("utf-16-le")
    source_path.write_bytes(original)
    worker = StrictTextEncodingNormalizer()
    with source_path.open("rb") as source, destination_path.open("wb") as output:
        disk_result = worker._transform(source, output)
        assert not source.closed and not output.closed
    with source_path.open("rb") as source:
        memory = BytesIO()
        memory_result = worker._transform(source, memory)
    assert disk_result == memory_result
    assert source_path.read_bytes() == original
    assert destination_path.read_bytes() == memory.getvalue() == text.encode("utf-8")


@pytest.mark.parametrize("chunk_size", [0, -1, True, False, 1.5, "1024", None])
def test_invalid_chunk_sizes_raise(chunk_size):
    with pytest.raises(ValueError, match="positive integer"):
        StrictTextEncodingNormalizer(chunk_size=chunk_size)


@pytest.mark.parametrize("declaration,error", [
    ("", ValueError), (" \t", ValueError), (1, TypeError), (b"utf-8", TypeError),
])
def test_invalid_declarations_are_rejected_at_request_boundary(declaration, error):
    with pytest.raises(error):
        NormalizationRequest(source=b"hello", output_path=None, declared_encoding=declaration)


def test_transform_trusts_stream_capabilities_supplied_by_io_handler():
    class Reader(BytesIO):
        def readable(self):
            pytest.fail("the I/O handler already provides a readable stream")

        def seekable(self):
            pytest.fail("the I/O handler already provides a seekable stream")

    class Output(BytesIO):
        def writable(self):
            pytest.fail("the I/O handler already provides a writable stream")

    with Reader("Café".encode("utf-16")) as source, Output() as output:
        report = StrictTextEncodingNormalizer(chunk_size=1)._transform(source, output)
        assert report.outcome is TextEncodingOutcome.CHANGED
        assert output.getvalue() == "Café".encode("utf-8")


@pytest.mark.parametrize("returned", [None, 0, -1, True, "1", 1.5, 1000000])
def test_invalid_write_counts_raise_instead_of_hanging_or_losing_bytes(returned):
    class Writer(_Writer):
        def write(self, content):
            return returned

    with pytest.raises(OSError, match="byte count"):
        StrictTextEncodingNormalizer()._transform(BytesIO(codecs.BOM_UTF8 + b"hello"), Writer())


@pytest.mark.parametrize("error", [
    OSError("disk full"),
    ValueError("destination quota exceeded"),
    UnicodeEncodeError("utf-8", "x", 0, 1, "destination error"),
])
def test_destination_errors_propagate_unchanged(error):
    class Writer(_Writer):
        def write(self, content):
            raise error

    source = BytesIO(codecs.BOM_UTF8 + b"hello")
    with pytest.raises(type(error)) as raised:
        StrictTextEncodingNormalizer()._transform(source, Writer())
    assert raised.value is error
    assert not source.closed


def test_source_io_errors_propagate_unchanged():
    error = OSError("source disconnected")

    class Reader(BytesIO):
        def read(self, size=-1):
            if size:
                raise error
            return b""

    output = _Writer()
    with pytest.raises(OSError) as raised:
        StrictTextEncodingNormalizer()._transform(Reader(), output)
    assert raised.value is error
    assert output.calls == 0
