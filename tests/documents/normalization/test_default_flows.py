"""Default selection covers allowed extensions without changing worker contracts."""

import codecs
import csv
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from io import BytesIO, StringIO

import pikepdf
import pytest
from PIL import Image

from raggae.documents.normalization import (
    Artifact,
    DocumentNormalizer,
    NormalizationRequest,
    StrictTextEncodingNormalizer,
)
from raggae.documents.normalization import coordinator
from raggae.documents.validation.shared import SUPPORTED_EXTENSIONS

from .test_coordinator import RecordingWorker
from .test_image_orientation import encoded as encoded_image
from .test_ooxml_active_content import MAIN, package, parts
from .test_pdf_active_content import add_all_features, encoded_pdf


PDF_WORKERS = (
    "PdfDecryptionNormalizer", "PdfActiveContentSanitizer", "PdfMetadataSanitizer",
)
OFFICE_WORKERS = ("OoxmlActiveContentSanitizer", "OoxmlMetadataSanitizer")
IMAGE_WORKERS = ("PillowImageOrientationNormalizer", "ImageMetadataSanitizer")
TEXT_WORKERS = ("StrictTextEncodingNormalizer",)
ROUTES = {
    "pdf": PDF_WORKERS,
    "docx": OFFICE_WORKERS,
    "xlsx": OFFICE_WORKERS,
    "pptx": OFFICE_WORKERS,
    "jpg": IMAGE_WORKERS,
    "jpeg": IMAGE_WORKERS,
    "png": IMAGE_WORKERS,
    "tif": IMAGE_WORKERS,
    "tiff": IMAGE_WORKERS,
    "html": TEXT_WORKERS + ("HtmlActiveContentSanitizer", "HtmlMetadataSanitizer"),
    "csv": TEXT_WORKERS + ("CsvFormulaSanitizer",),
    "txt": TEXT_WORKERS,
    "md": TEXT_WORKERS,
    "json": TEXT_WORKERS,
    "jsonl": TEXT_WORKERS,
}


@pytest.fixture
def recorded_defaults(monkeypatch):
    """Replace worker classes with lightweight classes, without parsing files."""
    created = []

    def replacement_class(worker_name):
        class FakeWorker(RecordingWorker):
            def __init__(self):
                super().__init__(worker_name, "no_change")
                created.append(self)

        return FakeWorker

    for worker_name in {name for names in ROUTES.values() for name in names}:
        monkeypatch.setattr(coordinator, worker_name, replacement_class(worker_name))
    return created


def test_recorded_defaults_preserve_worker_class_contract(recorded_defaults):
    for worker_name in {name for names in ROUTES.values() for name in names}:
        worker_class = getattr(coordinator, worker_name)
        assert isinstance(worker_class, type)
        worker = worker_class()
        assert isinstance(worker, worker_class)
        assert worker.name == worker_name
        assert recorded_defaults[-1] is worker


def test_default_catalog_covers_every_validation_allowed_extension():
    assert set(ROUTES) == SUPPORTED_EXTENSIONS


@pytest.mark.parametrize("extension", ROUTES)
@pytest.mark.parametrize("upper_case", [False, True])
def test_path_extension_selects_exact_ordered_chain(extension, upper_case, recorded_defaults, tmp_path):
    suffix = extension.upper() if upper_case else extension
    source = tmp_path / f"original.{suffix}"
    source.write_bytes(b"not read by test workers")
    request = NormalizationRequest(source=source, output_path=tmp_path / "wrong-output.pdf")

    result = DocumentNormalizer().normalize(request)

    assert [worker.name for worker in recorded_defaults] == list(ROUTES[extension])
    assert [step.worker for step in result.report.steps] == list(ROUTES[extension])
    assert result.outcome == "no_change"
    assert result.artifact is request.source
    assert request.source_format is None
    assert all(worker.requests[0].output_path is None for worker in recorded_defaults)
    assert not request.output_path.exists()


@pytest.mark.parametrize("source_format", ROUTES)
@pytest.mark.parametrize("artifact_input", [False, True])
def test_explicit_format_selects_memory_flow(source_format, artifact_input, recorded_defaults):
    source = Artifact(data=b"input") if artifact_input else b"input"
    request = NormalizationRequest(
        source=source, source_format=f" .{source_format.upper()} ", output_path=None
    )
    result = DocumentNormalizer().normalize(request)
    assert request.source_format == source_format
    assert [step.worker for step in result.report.steps] == list(ROUTES[source_format])
    assert result.artifact is request.source
    assert all(worker.requests[0].source_format == source_format for worker in recorded_defaults)


@pytest.mark.parametrize("path_name", ["source.pdf", "source.htm", "source"])
def test_explicit_format_overrides_extension(path_name, recorded_defaults, tmp_path):
    request = NormalizationRequest(source=tmp_path / path_name, source_format="png", output_path=None)
    result = DocumentNormalizer().normalize(request)
    assert [step.worker for step in result.report.steps] == list(IMAGE_WORKERS)


@pytest.mark.parametrize("source_kind", ["bytes", "artifact", "extensionless", "hidden"])
def test_missing_format_rejected_before_workers_or_output(source_kind, recorded_defaults, tmp_path):
    sources = {
        "bytes": b"input",
        "artifact": Artifact(data=b"input"),
        "extensionless": tmp_path / "source",
        "hidden": tmp_path / ".pdf",
    }
    destination = tmp_path / "final.pdf"
    with pytest.raises(ValueError, match="source_format is required"):
        DocumentNormalizer().normalize(
            NormalizationRequest(source=sources[source_kind], output_path=destination)
        )
    assert not recorded_defaults
    assert not destination.exists()


@pytest.mark.parametrize("source_format", ["htm", "exe", "zip", "odt", "unknown"])
@pytest.mark.parametrize("explicit_format", [False, True])
def test_unsupported_formats_rejected_without_fallback(source_format, explicit_format, recorded_defaults, tmp_path):
    request = NormalizationRequest(
        source=b"input" if explicit_format else tmp_path / f"absent.{source_format}",
        source_format=source_format if explicit_format else None,
        output_path=tmp_path / "final.txt",
    )
    with pytest.raises(ValueError, match="no default normalization flow"):
        DocumentNormalizer().normalize(request)
    assert not recorded_defaults
    assert not request.output_path.exists()


@pytest.mark.parametrize("source_format", ["html", "csv"])
@pytest.mark.parametrize("target_encoding", ["utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"])
def test_html_csv_target_restriction_precedes_workers_and_io(source_format, target_encoding, recorded_defaults, tmp_path):
    with pytest.raises(ValueError, match="require target_encoding='utf-8'"):
        DocumentNormalizer().normalize(NormalizationRequest(
            source=tmp_path / f"absent.{source_format}",
            target_encoding=target_encoding,
            output_path=tmp_path / "final.bin",
        ))
    assert not recorded_defaults
    assert not list(tmp_path.iterdir())


def test_none_workers_means_automatic_selection(recorded_defaults):
    result = DocumentNormalizer(workers=None).normalize(
        NormalizationRequest(source=b"input", source_format="pdf", output_path=None)
    )
    assert [step.worker for step in result.report.steps] == list(PDF_WORKERS)


@pytest.mark.parametrize("source_format", [None, "unknown", "html"])
def test_explicit_workers_bypass_selection_and_automatic_encoding_restrictions(source_format, recorded_defaults):
    request = NormalizationRequest(
        source=b"plain text", source_format=source_format,
        target_encoding="utf-16", output_path=None,
    )
    result = DocumentNormalizer(workers=(StrictTextEncodingNormalizer(),)).normalize(request)
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == "plain text".encode("utf-16")
    assert len(result.report.steps) == 1
    assert not recorded_defaults


def test_empty_explicit_chain_stays_noop_without_format(recorded_defaults):
    request = NormalizationRequest(source=b"input", output_path=None)
    result = DocumentNormalizer(workers=()).normalize(request)
    assert result.artifact is request.source
    assert result.outcome == "no_change"
    assert result.report.steps == ()
    assert not recorded_defaults


def test_same_normalizer_selects_fresh_flow_for_every_call(recorded_defaults):
    normalizer = DocumentNormalizer()
    first = normalizer.normalize(NormalizationRequest(source=b"input", source_format="pdf", output_path=None))
    second = normalizer.normalize(NormalizationRequest(source=b"input", source_format="csv", output_path=None))
    third = normalizer.normalize(NormalizationRequest(source=b"input", source_format="pdf", output_path=None))
    assert [step.worker for step in first.report.steps] == list(PDF_WORKERS)
    assert [step.worker for step in second.report.steps] == list(ROUTES["csv"])
    assert [step.worker for step in third.report.steps] == list(PDF_WORKERS)
    assert len(recorded_defaults) == 8
    assert len({id(worker) for worker in recorded_defaults}) == 8


@pytest.mark.parametrize("source_format,expected", [
    (None, None), ("pdf", "pdf"), (".PDF", "pdf"), (" HTML ", "html"),
    (" .JPG ", "jpg"), ("CUSTOM", "custom"),
])
def test_request_normalizes_optional_format_without_restricting_independent_workers(source_format, expected):
    request = NormalizationRequest(source=b"text", source_format=source_format, output_path=None)
    assert request.source_format == expected
    assert StrictTextEncodingNormalizer().normalize(request).outcome == "no_change"


@pytest.mark.parametrize("source_format,error", [
    ("", ValueError), (" \t", ValueError), (".", ValueError), (" . ", ValueError),
    (123, TypeError), (True, TypeError), (b"pdf", TypeError), (object(), TypeError),
])
def test_invalid_format_values_rejected_by_request(source_format, error):
    with pytest.raises(error, match="source_format"):
        NormalizationRequest(source=b"text", source_format=source_format, output_path=None)


def test_request_format_is_frozen_and_survives_internal_advancement():
    request = NormalizationRequest(
        source=b"original", source_format="html", output_path=None,
        declared_encoding="cp1252", password="secret",
    )
    advanced = replace(request, source=Artifact(data=b"next"))
    assert advanced.source_format == request.source_format == "html"
    assert advanced.declared_encoding == "cp1252"
    assert advanced.password == "secret"
    assert request.source.data == b"original"
    with pytest.raises(FrozenInstanceError):
        request.source_format = "pdf"


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("memory_source", [False, True])
def test_real_default_html_flow_preserves_text_and_removes_script_and_author(disk, memory_source, tmp_path):
    original = '<html><head><meta name="author" content="Alice"></head><body><script>alert(1)</script><p>Café</p></body></html>'.encode("cp1252")
    path = tmp_path / "original.html"
    path.write_bytes(original)
    destination = tmp_path / "normalized.html" if disk else None
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=original if memory_source else path,
        source_format="html" if memory_source else None,
        declared_encoding="cp1252", output_path=destination,
    ))
    assert result.outcome == "changed"
    assert result.artifact.path == destination
    text = result.artifact.read_bytes().decode("utf-8")
    assert "<p>Café</p>" in text
    assert "script" not in text and "Alice" not in text
    assert [step.report.outcome for step in result.report.steps] == ["changed"] * 3
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("disk", [False, True])
def test_real_default_csv_flow_normalizes_then_protects_formula_cells(disk, tmp_path):
    original = "name,value\nCafé,=1+1\ncount,-42\n".encode("cp1252")
    path = tmp_path / "original.csv"
    path.write_bytes(original)
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=path, output_path=tmp_path / "normalized.csv" if disk else None,
        declared_encoding="cp1252",
    ))
    assert result.outcome == "changed"
    rows = list(csv.reader(StringIO(result.artifact.read_bytes().decode("utf-8"))))
    assert rows == [["name", "value"], ["Café", "'=1+1"], ["count", "-42"]]
    assert [step.report.outcome for step in result.report.steps] == ["changed", "changed"]
    assert result.report.steps[1].report.changed_cells == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("source_format", ["txt", "md", "json", "jsonl"])
@pytest.mark.parametrize("target_encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"])
def test_other_text_defaults_allow_existing_encoding_targets(source_format, target_encoding):
    original = codecs.BOM_UTF16_LE + "plain text".encode("utf-16-le")
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=original, source_format=source_format, output_path=None,
        target_encoding=target_encoding,
    ))
    assert result.outcome in {"changed", "no_change"}
    assert result.artifact.read_bytes() == "plain text".encode(target_encoding)
    assert len(result.report.steps) == 1


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("encrypted", [None, "open-me"])
def test_real_default_pdf_flow_decrypts_before_removing_active_content_and_metadata(disk, encrypted, tmp_path):
    original = encoded_pdf(add_all_features, encrypted=encrypted)
    path = tmp_path / "original.pdf"
    path.write_bytes(original)
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=path, output_path=tmp_path / "normalized.pdf" if disk else None,
        password=encrypted,
    ))
    assert result.outcome == "changed"
    assert [step.report.outcome for step in result.report.steps] == [
        "no_change" if encrypted is None else "changed", "changed", "changed",
    ]
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes()), attempt_recovery=False) as actual:
        assert not actual.is_encrypted
        assert "/OpenAction" not in actual.Root
        assert "/Info" not in actual.trailer
        assert "/Metadata" not in actual.Root
        assert len(actual.pages) == 2
        assert actual.pages[0].obj.Contents.read_bytes() == b"q 1 0 0 rg 10 10 20 20 re f Q"
    assert path.read_bytes() == original
    assert "open-me" not in result.report.model_dump_json()


@pytest.mark.parametrize("password", [None, "", "wrong"])
def test_default_pdf_password_failure_stops_before_sanitizers(password, tmp_path):
    destination = tmp_path / "normalized.pdf"
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=encoded_pdf(encrypted="open-me"), source_format="pdf",
        password=password, output_path=destination,
    ))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert len(result.report.steps) == 1
    assert result.report.steps[0].worker == "normalization_pdf_decryption"
    assert not destination.exists()


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
@pytest.mark.parametrize("disk", [False, True])
def test_real_office_defaults_remove_macros_external_relationships_and_metadata(family, disk, tmp_path):
    payloads = parts(family, active=True)
    original = package(payloads)
    path = tmp_path / f"original.{family}"
    path.write_bytes(original)
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=path, output_path=tmp_path / f"normalized.{family}" if disk else None,
    ))
    assert result.outcome == "changed"
    assert [step.report.outcome for step in result.report.steps] == ["changed", "changed"]
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert archive.read(MAIN[family]) == payloads[MAIN[family]]
        assert "docProps/core.xml" not in archive.namelist()
        assert not any("vba" in name.casefold() for name in archive.namelist())
        assert not any(b"https://" in archive.read(name) for name in archive.namelist() if name.endswith(".rels"))
        assert archive.testzip() is None
    assert path.read_bytes() == original


@pytest.mark.parametrize("extension,image_format", [
    ("jpg", "JPEG"), ("jpeg", "JPEG"), ("png", "PNG"), ("tif", "TIFF"), ("tiff", "TIFF"),
])
@pytest.mark.parametrize("disk", [False, True])
def test_real_image_defaults_apply_orientation_before_removing_metadata(extension, image_format, disk, tmp_path):
    original = encoded_image(image_format, orientation=6)
    path = tmp_path / f"original.{extension}"
    path.write_bytes(original)
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=path, output_path=tmp_path / f"normalized.{extension}" if disk else None,
    ))
    assert result.outcome == "changed"
    assert [step.report.outcome for step in result.report.steps] == ["changed", "changed"]
    with Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        actual.load()
        assert actual.format == image_format
        assert actual.size == (2, 3)
        assert actual.getexif().get(274) in (None, 1)
        assert actual.getexif().get(315) is None
        if image_format == "PNG":
            assert not actual.text
    assert path.read_bytes() == original


@pytest.mark.parametrize("source_format,content", [
    ("txt", b"plain text"), ("html", b"<p>plain text</p>"), ("csv", b"name,value\nitem,42\n"),
])
def test_real_default_no_change_does_not_write_copy(source_format, content, tmp_path):
    destination = tmp_path / f"unused.{source_format}"
    request = NormalizationRequest(source=content, source_format=source_format, output_path=destination)
    result = DocumentNormalizer().normalize(request)
    assert result.outcome == "no_change"
    assert result.artifact is request.source
    assert all(step.report.outcome == "no_change" for step in result.report.steps)
    assert not destination.exists()


def test_wrong_extension_does_not_invoke_content_detection_or_another_flow():
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=b"<p>HTML is not a PDF</p>", source_format="pdf", output_path=None,
    ))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert len(result.report.steps) == 1
    assert result.report.steps[0].worker == "normalization_pdf_decryption"


def test_default_chain_encoding_failure_returns_no_partial_copy(tmp_path):
    destination = tmp_path / "normalized.html"
    result = DocumentNormalizer().normalize(NormalizationRequest(
        source=b"\xffinvalid", source_format="html", output_path=destination,
    ))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert len(result.report.steps) == 1
    assert result.report.steps[0].report.issues
    assert not destination.exists()


def test_default_normalizer_is_safe_for_independent_concurrent_calls():
    normalizer = DocumentNormalizer()

    def run(index):
        source_format = "csv" if index % 2 else "html"
        data = b"value\n=1+1\n" if source_format == "csv" else b"<script>example</script><p>keep</p>"
        result = normalizer.normalize(NormalizationRequest(source=data, source_format=source_format, output_path=None))
        return result.artifact.read_bytes(), len(result.report.steps)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(12)))
    assert results == [(b"value\n'=1+1\n", 2) if index % 2 else (b"<p>keep</p>", 3) for index in range(12)]
