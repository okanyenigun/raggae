"""The minimal public request, artifact, and shared-handler API."""

import codecs
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest

from raggae.documents.normalization import (
    Artifact,
    SharedInputOutputHandler,
    StrictTextEncodingNormalizer,
    TextEncodingFinding,
    TextEncodingOutcome,
    TextEncodingReport,
    NormalizationRequest,
    NormalizationResult,
)


@pytest.mark.parametrize("source_kind", ["file", "bytes"])
@pytest.mark.parametrize("disk", [False, True])
def test_public_worker_returns_content_without_caller_streams(source_kind, disk, tmp_path):
    text = "name,city\r\nJosé,İstanbul\r\n"
    original = codecs.BOM_UTF16_LE + text.encode("utf-16-le")
    path = tmp_path / "original.csv"
    if source_kind == "file":
        path.write_bytes(original)
        source = str(path)
    else:
        source = original
    destination = tmp_path / "normalized.csv" if disk else None
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(
        NormalizationRequest(source=source, output_path=destination)
    )
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.report.bom_removed
    assert result.report.source_encoding == "utf-16-le"
    assert result.artifact.read_bytes() == text.encode("utf-8")
    assert result.artifact.path == destination
    assert result.artifact.data == (None if disk else text.encode("utf-8"))
    assert result.report.issues == ()
    if source_kind == "file":
        assert path.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("source_kind", ["file", "bytes"])
@pytest.mark.parametrize("disk", [False, True])
def test_no_change_returns_original_without_creating_an_output_copy(source_kind, disk, tmp_path):
    original = "already UTF-8: café\r\n".encode("utf-8")
    path = tmp_path / "original.csv"
    if source_kind == "file":
        path.write_bytes(original)
        source = path
    else:
        source = original
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=source, output_path=tmp_path / "normalized.csv" if disk else None
    ))
    assert result.outcome is TextEncodingOutcome.NO_CHANGE
    assert result.artifact.read_bytes() == original
    assert result.artifact.path == (path if source_kind == "file" else None)
    assert sorted(tmp_path.iterdir()) == ([path] if source_kind == "file" else [])


@pytest.mark.parametrize("disk", [False, True])
def test_failure_discards_partial_output_and_returns_no_artifact(disk, tmp_path):
    secret = "PRIVATE CONTENT"
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(
        source=codecs.BOM_UTF16_LE + secret.encode("utf-16-le") + b"x",
        output_path=tmp_path / "normalized.csv" if disk else None,
    ))
    assert result.outcome is TextEncodingOutcome.FAILED
    assert result.artifact is None
    assert result.report.issues[0].code is TextEncodingFinding.DECODE_FAILED
    assert secret not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("disk", [False, True])
def test_changed_empty_text_is_still_a_valid_document(disk, tmp_path):
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=codecs.BOM_UTF8, output_path=tmp_path / "empty.txt" if disk else None
    ))
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.artifact.read_bytes() == b""


def test_legacy_encoding_is_a_request_parameter():
    request = NormalizationRequest(
        source=b"caf\xe9", output_path=None, declared_encoding=" cp1252 "
    )
    assert request.declared_encoding == "cp1252"
    result = StrictTextEncodingNormalizer().normalize(request)
    assert result.outcome is TextEncodingOutcome.CHANGED
    assert result.artifact.data == "café".encode("utf-8")


@pytest.mark.parametrize("disk", [False, True])
def test_returned_artifact_can_directly_feed_another_call(disk, tmp_path):
    worker = StrictTextEncodingNormalizer()
    first = worker.normalize(NormalizationRequest(
        source=codecs.BOM_UTF8 + b"text",
        output_path=tmp_path / "first.txt" if disk else None,
    ))
    second = worker.normalize(NormalizationRequest(
        source=first.artifact, output_path=tmp_path / "unnecessary.txt"
    ))
    assert second.outcome is TextEncodingOutcome.NO_CHANGE
    assert second.artifact is first.artifact
    assert second.artifact.read_bytes() == b"text"
    assert not (tmp_path / "unnecessary.txt").exists()
    if disk:
        assert first.artifact.path.exists()


def test_original_path_cannot_be_used_as_destination(tmp_path):
    path = tmp_path / "original.txt"
    original = codecs.BOM_UTF8 + b"text"
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        StrictTextEncodingNormalizer().normalize(
            NormalizationRequest(source=path, output_path=path)
        )
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_worker_delegates_resource_handling_to_the_shared_handler(monkeypatch):
    calls = []
    execute = SharedInputOutputHandler.execute

    def record_call(self, **kwargs):
        calls.append((kwargs["source"], kwargs["output_path"]))
        return execute(self, **kwargs)

    monkeypatch.setattr(SharedInputOutputHandler, "execute", record_call)
    request = NormalizationRequest(source=codecs.BOM_UTF8 + b"text", output_path=None)
    result = StrictTextEncodingNormalizer().normalize(request)
    assert calls == [(request.source, None)]
    assert result.artifact.read_bytes() == b"text"


def test_shared_worker_is_safe_to_use_for_independent_concurrent_calls():
    worker = StrictTextEncodingNormalizer(chunk_size=1)

    def run(index):
        text = f"document {index}: café"
        result = worker.normalize(NormalizationRequest(
            source=codecs.BOM_UTF16_LE + text.encode("utf-16-le"), output_path=None
        ))
        return result.artifact.read_bytes()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(12)))
    assert results == [f"document {index}: café".encode("utf-8") for index in range(12)]


@pytest.mark.parametrize("declaration,error", [
    ("", ValueError), (" \t", ValueError), (1, TypeError), (b"utf-8", TypeError),
])
def test_invalid_encoding_parameters_are_rejected_before_io(declaration, error):
    with pytest.raises(error):
        NormalizationRequest(source=b"text", output_path=None, declared_encoding=declaration)


@pytest.mark.parametrize("invalid_request", ["source.txt", {}, BytesIO(b"text"), None])
def test_public_worker_requires_a_request_model(invalid_request):
    with pytest.raises(TypeError, match="NormalizationRequest"):
        StrictTextEncodingNormalizer().normalize(invalid_request)


@pytest.mark.parametrize("source,output_path,error", [
    (object(), None, TypeError),
    (bytearray(b"text"), None, TypeError),
    (b"text", object(), TypeError),
    ("", None, ValueError),
    (b"text", "", ValueError),
])
def test_invalid_source_or_destination_is_rejected(source, output_path, error):
    with pytest.raises(error):
        NormalizationRequest(source=source, output_path=output_path)


def test_output_preference_must_be_explicit():
    with pytest.raises(TypeError):
        NormalizationRequest(source=b"text")


def test_request_and_result_are_read_only_and_report_contains_no_document_bytes():
    request = NormalizationRequest(source=codecs.BOM_UTF8 + b"secret text", output_path=None)
    result = StrictTextEncodingNormalizer().normalize(request)
    assert result.outcome is result.report.outcome
    assert "secret text" not in result.report.model_dump_json()
    assert "secret text" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.artifact = None
    with pytest.raises(FrozenInstanceError):
        request.output_path = None


@pytest.mark.parametrize("kwargs,error", [
    ({"report": "report", "artifact": None}, TypeError),
    ({"report": TextEncodingReport(outcome="changed", source_encoding="utf-8"), "artifact": object()}, TypeError),
    ({"report": TextEncodingReport(outcome="changed", source_encoding="utf-8"), "artifact": None}, ValueError),
])
def test_result_rejects_invalid_or_missing_successful_artifacts(kwargs, error):
    with pytest.raises(error):
        NormalizationResult(**kwargs)


def test_failed_result_cannot_claim_a_usable_document():
    failed = StrictTextEncodingNormalizer().normalize(
        NormalizationRequest(source=b"\xff", output_path=None)
    )
    with pytest.raises(ValueError, match="failed result"):
        NormalizationResult(report=failed.report, artifact=Artifact(data=b"bad"))
