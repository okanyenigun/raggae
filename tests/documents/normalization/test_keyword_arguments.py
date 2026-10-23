"""All built-in workers and the coordinator support the same two call forms."""

import importlib
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Literal, Unpack, get_args, get_origin, get_overloads, get_type_hints

import pikepdf
import pytest
from pydantic import BaseModel

from raggae.documents.normalization import (
    Artifact,
    CsvFormulaSanitizer,
    DocumentNormalizer,
    DocumentToHtmlConverter,
    HtmlActiveContentSanitizer,
    HtmlMetadataSanitizer,
    ImageMetadataSanitizer,
    LibreOfficeToPdfConverter,
    NormalizationRequest,
    OoxmlActiveContentSanitizer,
    OoxmlMetadataSanitizer,
    PdfActiveContentSanitizer,
    PdfDecryptionNormalizer,
    PdfMetadataSanitizer,
    PillowImageOrientationNormalizer,
    PillowImageToPdfConverter,
    SharedInputOutputHandler,
    StrictTextEncodingNormalizer,
)
from raggae.documents.normalization.format_conversion import pandoc
from raggae.documents.normalization.request import NormalizationArguments, resolve_request

from .test_coordinator import RecordingWorker
from .test_image_orientation import encoded as encoded_image
from .test_ooxml_active_content import package, parts
from .test_pdf_active_content import add_all_features, encoded_pdf


WORKERS = (
    StrictTextEncodingNormalizer,
    PillowImageOrientationNormalizer,
    PdfDecryptionNormalizer,
    PdfActiveContentSanitizer,
    OoxmlActiveContentSanitizer,
    HtmlActiveContentSanitizer,
    CsvFormulaSanitizer,
    PdfMetadataSanitizer,
    OoxmlMetadataSanitizer,
    HtmlMetadataSanitizer,
    ImageMetadataSanitizer,
    PillowImageToPdfConverter,
    DocumentToHtmlConverter,
    LibreOfficeToPdfConverter,
)
ENTRY_POINTS = (*WORKERS, DocumentNormalizer)

CASES = (
    (StrictTextEncodingNormalizer, lambda: b"caf\xe9", {"declared_encoding": "cp1252"}),
    (PillowImageOrientationNormalizer, encoded_image, {}),
    (PdfDecryptionNormalizer, lambda: encoded_pdf(encrypted="open-me"), {"password": "open-me"}),
    (PdfActiveContentSanitizer, lambda: encoded_pdf(add_all_features), {}),
    (OoxmlActiveContentSanitizer, lambda: package(parts(active=True)), {}),
    (HtmlActiveContentSanitizer, lambda: b"<script>example</script><p>keep</p>", {}),
    (CsvFormulaSanitizer, lambda: b"value\n=1+1\n", {}),
    (PdfMetadataSanitizer, encoded_pdf, {}),
    (OoxmlMetadataSanitizer, lambda: package(parts()), {}),
    (HtmlMetadataSanitizer, lambda: b'<meta name="author" content="Alice"><p>keep</p>', {}),
    (ImageMetadataSanitizer, lambda: encoded_image(orientation=1), {}),
    (PillowImageToPdfConverter, lambda: encoded_image(orientation=1), {}),
    (DocumentToHtmlConverter, lambda: b"opaque native-converter input", {}),
    (LibreOfficeToPdfConverter, lambda: b"opaque native-converter input", {}),
)


@pytest.fixture
def fake_native_tools(monkeypatch):
    """Exercise converter call boundaries without launching external programs."""
    calls = []
    monkeypatch.setattr(pandoc.shutil, "which", lambda name: f"/fake/bin/{name}")

    def run(arguments, **settings):
        calls.append((arguments, settings))
        if "--to=html5" in arguments:
            destination = Path(next(value.removeprefix("--output=") for value in arguments if value.startswith("--output=")))
            destination.write_bytes(b"<!DOCTYPE html><html><body>converted</body></html>")
        else:
            assert "--convert-to" in arguments
            directory = Path(arguments[arguments.index("--outdir") + 1])
            (directory / "source.pdf").write_bytes(b"%PDF-1.7\nmock native output\n%%EOF\n")
        return subprocess.CompletedProcess(arguments, 0)

    # Both converter modules use the same standard-library subprocess/shutil modules.
    monkeypatch.setattr(pandoc.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("worker_type,source_factory,settings", CASES, ids=[case[0].__name__ for case in CASES])
@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("file_source", [False, True])
def test_every_worker_has_equivalent_request_and_keyword_results(
    worker_type, source_factory, settings, disk, file_source, fake_native_tools, tmp_path
):
    original = source_factory()
    original_path = tmp_path / "original.bin"
    original_path.write_bytes(original)
    source = original_path if file_source else original
    worker = worker_type()
    request = NormalizationRequest(
        source=source, output_path=tmp_path / "request-output.bin" if disk else None,
        **settings,
    )
    from_request = worker.normalize(request)
    from_keywords = worker.normalize(
        source=source, output_path=tmp_path / "keyword-output.bin" if disk else None,
        **settings,
    )

    assert from_request.outcome == from_keywords.outcome == "changed"
    assert from_request.report == from_keywords.report
    request_bytes = from_request.artifact.read_bytes()
    keyword_bytes = from_keywords.artifact.read_bytes()
    if worker_type in (
        PdfDecryptionNormalizer, PdfActiveContentSanitizer,
        PdfMetadataSanitizer, PillowImageToPdfConverter,
    ):
        # Separate saves may generate different PDF identifiers or timestamps.
        # Compare page contents rather than requiring identical serialization.
        with pikepdf.Pdf.open(BytesIO(request_bytes)) as first_pdf, pikepdf.Pdf.open(BytesIO(keyword_bytes)) as second_pdf:
            assert not first_pdf.is_encrypted and not second_pdf.is_encrypted
            assert len(first_pdf.pages) == len(second_pdf.pages)
            for first_page, second_page in zip(first_pdf.pages, second_pdf.pages):
                assert list(first_page.MediaBox) == list(second_page.MediaBox)
                assert first_page.Contents.read_bytes() == second_page.Contents.read_bytes()
                first_images = first_page.get_images()
                second_images = second_page.get_images()
                assert set(first_images) == set(second_images)
                for name in first_images:
                    assert first_images[name].read_raw_bytes() == second_images[name].read_raw_bytes()
    else:
        assert request_bytes == keyword_bytes
    assert from_keywords.artifact.path == (tmp_path / "keyword-output.bin" if disk else None)
    assert request.source.read_bytes() == original_path.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))
    if worker_type in (DocumentToHtmlConverter, LibreOfficeToPdfConverter):
        assert len(fake_native_tools) == 2


class NoChangeReport(BaseModel):
    outcome: Literal["no_change"] = "no_change"


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_every_entry_point_uses_shared_resolution_for_all_common_fields(entry_point, monkeypatch):
    resolved = []
    module = importlib.import_module(entry_point.normalize.__module__)

    def record_resolution(request, arguments):
        result = resolve_request(request, arguments)
        resolved.append(result)
        return result

    def no_change_io(self, *, source, output_path, transform):
        return NoChangeReport(), source

    monkeypatch.setattr(module, "resolve_request", record_resolution)
    monkeypatch.setattr(SharedInputOutputHandler, "execute", no_change_io)
    source = Artifact(data=b"text")
    arguments = {
        "source": source, "output_path": None, "declared_encoding": " cp1252 ",
        "target_encoding": "utf-16", "password": b" exact secret ",
        "source_format": " .TXT ",
    }
    request = NormalizationRequest(**arguments)
    worker = entry_point()
    first = worker.normalize(request)
    second = worker.normalize(**arguments)

    assert len(resolved) == 2
    assert resolved[0] is request
    assert resolved[1] is not request
    assert resolved[0] == resolved[1]
    assert resolved[1].source is source
    assert resolved[1].declared_encoding == "cp1252"
    assert resolved[1].target_encoding == "utf-16"
    assert resolved[1].password == b" exact secret "
    assert resolved[1].source_format == "txt"
    assert first.artifact is second.artifact is source
    assert first.outcome == second.outcome == "no_change"
    assert "exact secret" not in second.report.model_dump_json()


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("field", ["source", "output_path", "declared_encoding", "target_encoding", "password", "source_format"])
def test_every_entry_point_rejects_mixed_inputs_before_io(entry_point, field, monkeypatch):
    request = NormalizationRequest(source=b"text", output_path=None, source_format="txt")

    def forbidden_io(*args, **kwargs):
        pytest.fail("mixed input must be rejected before I/O")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", forbidden_io)
    with pytest.raises(TypeError, match="not both"):
        entry_point().normalize(request, **{field: getattr(request, field)})


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("arguments", [{}, {"source": b"text"}, {"output_path": None}])
def test_every_entry_point_requires_source_and_output_preference(entry_point, arguments, monkeypatch):
    def forbidden_io(*args, **kwargs):
        pytest.fail("missing fields must be rejected before I/O")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", forbidden_io)
    with pytest.raises(TypeError):
        entry_point().normalize(**arguments)


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_every_entry_point_rejects_unknown_keywords(entry_point, monkeypatch):
    def forbidden_io(*args, **kwargs):
        pytest.fail("unknown keywords must be rejected before I/O")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", forbidden_io)
    with pytest.raises(TypeError, match="declared_encodng"):
        entry_point().normalize(source=b"text", output_path=None, declared_encodng="cp1252")


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("field,value", [("target_encoding", "UTF8"), ("password", 123), ("source_format", "")])
def test_every_entry_point_keeps_request_validation_before_io(entry_point, field, value, monkeypatch):
    def forbidden_io(*args, **kwargs):
        pytest.fail("invalid configuration must be rejected before I/O")

    monkeypatch.setattr(SharedInputOutputHandler, "execute", forbidden_io)
    with pytest.raises((TypeError, ValueError), match=field):
        entry_point().normalize(source=b"text", output_path=None, **{field: value})


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_every_entry_point_exposes_typed_keywords_and_the_same_precise_return(entry_point):
    overloads = get_overloads(entry_point.normalize)
    assert len(overloads) == 2
    request_form, keyword_form = map(get_type_hints, overloads)
    assert request_form["request"] is NormalizationRequest
    assert get_origin(keyword_form["kwargs"]) is Unpack
    assert get_args(keyword_form["kwargs"]) == (NormalizationArguments,)
    assert keyword_form["return"] == request_form["return"]


@pytest.mark.parametrize("disk", [False, True])
def test_automatic_coordinator_has_equivalent_keyword_and_request_flows(disk, tmp_path):
    original = '<meta name="author" content="Alice"><script>example</script><p>Café</p>'.encode("cp1252")
    path = tmp_path / "original.html"
    path.write_bytes(original)
    normalizer = DocumentNormalizer()
    from_request = normalizer.normalize(NormalizationRequest(
        source=path, declared_encoding="cp1252",
        output_path=tmp_path / "request.html" if disk else None,
    ))
    from_keywords = normalizer.normalize(
        source=path, declared_encoding="cp1252",
        output_path=tmp_path / "keywords.html" if disk else None,
    )
    assert from_keywords.report == from_request.report
    assert from_keywords.outcome == "changed"
    assert len(from_keywords.report.steps) == 3
    assert from_keywords.artifact.read_bytes() == from_request.artifact.read_bytes() == "<p>Café</p>".encode("utf-8")
    assert path.read_bytes() == original


def test_automatic_coordinator_accepts_memory_format_and_password_keywords():
    original = encoded_pdf(add_all_features, encrypted="open-me")
    result = DocumentNormalizer().normalize(
        source=original, source_format="pdf", password="open-me", output_path=None,
    )
    assert result.outcome == "changed"
    assert [step.report.outcome for step in result.report.steps] == ["changed"] * 3
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert not actual.is_encrypted
        assert "/OpenAction" not in actual.Root
        assert "/Info" not in actual.trailer
        assert len(actual.pages) == 2


def test_coordinator_keywords_still_forward_requests_to_custom_workers(tmp_path):
    first = RecordingWorker("first", suffix=b"!")
    last = RecordingWorker("last", "no_change")
    destination = tmp_path / "final.bin"
    result = DocumentNormalizer(workers=(first, last)).normalize(
        source=b"original", output_path=destination, declared_encoding="cp1252",
        target_encoding="utf-16", password="secret", source_format="custom",
    )
    assert result.outcome == "changed"
    assert destination.read_bytes() == b"original!"
    assert len(result.report.steps) == 2
    for worker in (first, last):
        request = worker.requests[0]
        assert isinstance(request, NormalizationRequest)
        assert request.output_path is None
        assert request.declared_encoding == "cp1252"
        assert request.target_encoding == "utf-16"
        assert request.password == "secret"
        assert request.source_format == "custom"
    assert last.requests[0].source is first.results[0].artifact


def test_empty_explicit_coordinator_accepts_keywords_without_format():
    source = Artifact(data=b"original")
    result = DocumentNormalizer(workers=()).normalize(source=source, output_path=None)
    assert result.outcome == "no_change"
    assert result.artifact is source
    assert result.report.steps == ()


@pytest.mark.parametrize("worker_type", [DocumentToHtmlConverter, LibreOfficeToPdfConverter])
def test_native_converter_failure_is_identical_for_both_call_forms(worker_type, monkeypatch, tmp_path):
    monkeypatch.setattr(pandoc.shutil, "which", lambda name: None)
    worker = worker_type()
    request = NormalizationRequest(source=b"input", output_path=tmp_path / "request-output")
    first = worker.normalize(request)
    second = worker.normalize(source=b"input", output_path=tmp_path / "keyword-output")
    assert first.report == second.report
    assert first.outcome == second.outcome == "failed"
    assert first.artifact is second.artifact is None
    assert not list(tmp_path.iterdir())
