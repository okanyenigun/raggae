"""The prototype's two call forms share one request and transformation path."""

import codecs
from concurrent.futures import ThreadPoolExecutor
from inspect import Parameter, signature
from typing import Unpack, get_args, get_origin, get_overloads, get_type_hints

import pytest

from raggae.documents.normalization import (
    Artifact,
    DocumentNormalizer,
    NormalizationRequest,
    NormalizationResult,
    NormalizationWorker,
    SharedInputOutputHandler,
    StrictTextEncodingNormalizer,
    TextEncodingReport,
)
from raggae.documents.normalization.request import NormalizationArguments, resolve_request


@pytest.mark.parametrize("source_kind", ["path", "str", "bytes", "file_artifact", "memory_artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_request_and_keyword_calls_have_identical_content_and_reports(source_kind, disk, tmp_path):
    original = "item,price\r\nCafé,€12\r\n".encode("cp1252")
    source_path = tmp_path / "original.csv"
    source_path.write_bytes(original)
    sources = {
        "path": source_path,
        "str": str(source_path),
        "bytes": original,
        "file_artifact": Artifact(path=source_path),
        "memory_artifact": Artifact(data=original),
    }
    worker = StrictTextEncodingNormalizer(chunk_size=1)
    request = NormalizationRequest(
        source=sources[source_kind], declared_encoding="cp1252",
        output_path=tmp_path / "from-request.csv" if disk else None,
    )
    from_request = worker.normalize(request)
    from_keywords = worker.normalize(
        source=sources[source_kind], declared_encoding="cp1252",
        output_path=tmp_path / "from-keywords.csv" if disk else None,
    )

    assert from_request.outcome == from_keywords.outcome == "changed"
    assert from_request.report == from_keywords.report
    assert from_request.artifact.read_bytes() == from_keywords.artifact.read_bytes()
    assert from_keywords.artifact.read_bytes() == original.decode("cp1252").encode("utf-8")
    assert source_path.read_bytes() == original
    assert request.source.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("target_encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"])
def test_keyword_calls_support_every_existing_target(target_encoding):
    result = StrictTextEncodingNormalizer().normalize(
        source=codecs.BOM_UTF16_LE + "plain text".encode("utf-16-le"),
        output_path=None, target_encoding=target_encoding,
    )
    assert result.artifact.read_bytes() == "plain text".encode(target_encoding)
    assert isinstance(result.report, TextEncodingReport)


def test_no_change_preserves_existing_artifact_identity(tmp_path):
    source = Artifact(data=b"already UTF-8")
    destination = tmp_path / "unused.txt"
    result = StrictTextEncodingNormalizer().normalize(source=source, output_path=destination)
    assert result.outcome == "no_change"
    assert result.artifact is source
    assert not destination.exists()


def test_document_failure_still_returns_no_artifact_or_partial_file(tmp_path):
    destination = tmp_path / "failed.txt"
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(
        source=codecs.BOM_UTF16_LE + "private text".encode("utf-16-le") + b"x",
        output_path=destination,
    )
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.issues
    assert "private text" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,value", [
    ("source", b"original"), ("output_path", None),
    ("declared_encoding", None), ("target_encoding", "utf-8"),
    ("password", None), ("source_format", None), ("unexpected", "value"),
])
def test_mixing_a_request_with_any_keyword_field_is_rejected_before_io(field, value, monkeypatch):
    request = NormalizationRequest(source=b"original", output_path=None)

    def unexpected_io(*args, **kwargs):
        pytest.fail("mixed input must be rejected before opening files")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", unexpected_io)
    with pytest.raises(TypeError, match="not both"):
        StrictTextEncodingNormalizer().normalize(request, **{field: value})
    assert request.source.data == b"original"


@pytest.mark.parametrize("arguments", [
    {}, {"source": b"text"}, {"output_path": None}, {"target_encoding": "utf-16"},
])
def test_missing_required_fields_rejected_before_io(arguments, monkeypatch):
    def unexpected_io(*args, **kwargs):
        pytest.fail("missing fields must be rejected before opening files")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", unexpected_io)
    with pytest.raises(TypeError):
        StrictTextEncodingNormalizer().normalize(**arguments)


@pytest.mark.parametrize("field,value", [
    ("source", object()), ("source", bytearray(b"text")),
    ("source", ""), ("output_path", ""), ("output_path", object()),
    ("declared_encoding", "UTF8"), ("declared_encoding", ""),
    ("declared_encoding", 123), ("target_encoding", "latin-1"),
    ("target_encoding", None), ("password", 123),
    ("source_format", ""), ("source_format", 123),
])
def test_keyword_validation_is_exactly_the_request_models_validation(field, value, monkeypatch):
    arguments = {"source": b"text", "output_path": None, field: value}
    with pytest.raises((TypeError, ValueError)) as expected:
        NormalizationRequest(**arguments)

    def unexpected_io(*args, **kwargs):
        pytest.fail("invalid configuration must be rejected before opening files")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", unexpected_io)
    with pytest.raises(type(expected.value)) as actual:
        StrictTextEncodingNormalizer().normalize(**arguments)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("field", ["declared_encodng", "output", "chunk_size", "format"])
def test_unknown_keywords_rejected_by_request_constructor(field, monkeypatch):
    def unexpected_io(*args, **kwargs):
        pytest.fail("unknown fields must be rejected before opening files")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", unexpected_io)
    with pytest.raises(TypeError, match=field):
        StrictTextEncodingNormalizer().normalize(source=b"text", output_path=None, **{field: 1})


def test_existing_request_passes_through_without_reconstruction(monkeypatch):
    request = NormalizationRequest(source=b"text", output_path=None)

    def unexpected_constructor(*args, **kwargs):
        pytest.fail("an existing request must be reused")

    monkeypatch.setattr(NormalizationRequest, "__init__", unexpected_constructor)
    assert resolve_request(request, {}) is request
    result = StrictTextEncodingNormalizer().normalize(request=request)
    assert result.artifact is request.source


def test_keyword_resolution_uses_request_defaults_and_normalization():
    request = resolve_request(None, {
        "source": b"caf\xe9", "output_path": None,
        "declared_encoding": " cp1252 ", "source_format": " .CSV ",
        "password": b" exact secret ",
    })
    assert isinstance(request, NormalizationRequest)
    assert request.source.data == b"caf\xe9"
    assert request.target_encoding == "utf-8"
    assert request.declared_encoding == "cp1252"
    assert request.source_format == "csv"
    assert request.password == b" exact secret "
    assert "exact secret" not in repr(request)


def test_ignored_common_fields_remain_accepted_and_secrets_are_not_reported():
    result = StrictTextEncodingNormalizer().normalize(
        source=b"caf\xe9", output_path=None, declared_encoding="cp1252",
        source_format="csv", password="unused secret",
    )
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == "café".encode("utf-8")
    assert "unused secret" not in result.report.model_dump_json()
    assert "unused secret" not in repr(result)


def test_kwargs_do_not_change_source_overwrite_protection(tmp_path):
    source = tmp_path / "original.txt"
    original = codecs.BOM_UTF8 + b"text"
    source.write_bytes(original)
    with pytest.raises(FileExistsError):
        StrictTextEncodingNormalizer().normalize(source=source, output_path=source)
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_kwargs_do_not_hide_file_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        StrictTextEncodingNormalizer().normalize(source=tmp_path / "absent.txt", output_path=None)


def test_keyword_types_and_required_keys_match_the_request_constructor():
    hints = get_type_hints(NormalizationRequest.__init__)
    hints.pop("return")
    assert get_type_hints(NormalizationArguments) == hints
    required = {
        name for name, parameter in signature(NormalizationRequest).parameters.items()
        if parameter.default is Parameter.empty
    }
    assert NormalizationArguments.__required_keys__ == required == {"source", "output_path"}


def test_two_overloads_keep_the_precise_return_type_and_typed_keyword_fields():
    overloads = get_overloads(StrictTextEncodingNormalizer.normalize)
    assert len(overloads) == 2
    request_form, keyword_form = map(get_type_hints, overloads)
    assert request_form["request"] is NormalizationRequest
    assert get_origin(keyword_form["kwargs"]) is Unpack
    assert get_args(keyword_form["kwargs"]) == (NormalizationArguments,)
    for hints in (request_form, keyword_form):
        assert get_origin(hints["return"]) is NormalizationResult
        assert get_args(hints["return"]) == (TextEncodingReport,)


def test_request_protocol_and_existing_coordinator_calls_still_work():
    worker: NormalizationWorker = StrictTextEncodingNormalizer()
    assert isinstance(worker, NormalizationWorker)
    request = NormalizationRequest(source=codecs.BOM_UTF8 + b"text", output_path=None)
    result = DocumentNormalizer(workers=(worker,)).normalize(request)
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == b"text"
    assert result.report.steps[0].report.bom_removed is True


def test_one_worker_supports_both_call_forms_concurrently():
    worker = StrictTextEncodingNormalizer(chunk_size=1)

    def run(index):
        text = f"document {index}: café"
        arguments = {
            "source": text.encode("cp1252"), "output_path": None,
            "declared_encoding": "cp1252",
        }
        result = worker.normalize(**arguments) if index % 2 else worker.normalize(NormalizationRequest(**arguments))
        return result.artifact.read_bytes()

    with ThreadPoolExecutor(max_workers=4) as executor:
        actual = list(executor.map(run, range(12)))
    assert actual == [f"document {index}: café".encode("utf-8") for index in range(12)]
