"""Saved PDFs prove metadata absence and preservation of ordinary content."""

from io import BytesIO

import pikepdf
import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    PdfMetadataFinding,
    PdfMetadataIssue,
    NormalizationWorker,
    PdfMetadataReport,
    PdfMetadataSanitizer,
    PdfDecryptionNormalizer,
)


def encoded_pdf(*, info=True, xmp=True, pages=2, user=None, extras=False):
    with pikepdf.Pdf.new() as document, BytesIO() as output:
        for _ in range(pages):
            page = document.add_blank_page(page_size=(240, 360))
            page.obj.Contents = document.make_stream(b"q 1 0 0 rg 10 10 20 20 re f Q")
        if info:
            document.docinfo["/Author"] = "Private author"
            document.docinfo["/Title"] = "Private title"
            document.docinfo["/CustomField"] = "Private custom value"
        if xmp:
            metadata = document.make_stream(
                b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
                b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                b'dc:title="Private XMP title"/></rdf:RDF></x:xmpmeta>'
            )
            metadata.Type = pikepdf.Name.Metadata
            metadata.Subtype = pikepdf.Name.XML
            document.Root.Metadata = metadata
        if extras:
            document.Root.OpenAction = pikepdf.Dictionary(
                S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('Keep active content')"),
            )
            document.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([
                document.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Signature"))),
            ]))
            document.pages[0].obj.Metadata = document.make_stream(b"Keep page-level metadata")
        encryption = None if user is None else pikepdf.Encryption(user=user, owner="owner")
        document.save(output, encryption=encryption, fix_metadata_version=False)
        return output.getvalue()


@pytest.mark.parametrize("info,xmp", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_metadata_removed_and_page_content_retained(info, xmp, source_kind, disk, tmp_path):
    original = encoded_pdf(info=info, xmp=xmp)
    source = original
    if source_kind == "path":
        path = tmp_path / "input.bin"
        path.write_bytes(original)
        source = str(path)
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.pdf" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = PdfMetadataSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.document_info_removed is info
    assert result.report.xmp_removed is xmp
    assert result.report.page_count == 2
    assert result.report.issues == ()
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes()), attempt_recovery=False) as actual:
        assert "/Info" not in actual.trailer
        assert "/Metadata" not in actual.Root
        assert not actual.is_encrypted
        assert len(actual.pages) == 2
        for page in actual.pages:
            assert list(page.obj.MediaBox) == [0, 0, 240, 360]
            assert page.obj.Contents.read_bytes() == b"q 1 0 0 rg 10 10 20 20 re f Q"
    assert "Private" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert PdfMetadataReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("disk", [False, True])
def test_no_metadata_returns_exact_original_without_creating_info(disk, tmp_path):
    original = Artifact(data=encoded_pdf(info=False, xmp=False))
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(
        source=original, output_path=tmp_path / "unused.pdf" if disk else None,
    ))
    assert result.outcome == "no_change"
    assert result.artifact is original
    assert not result.report.document_info_removed
    assert not result.report.xmp_removed
    assert not list(tmp_path.iterdir())


def test_idempotent_and_shared_settings_ignored():
    worker = PdfMetadataSanitizer()
    first = worker.normalize(NormalizationRequest(
        source=Artifact(data=encoded_pdf(), password="unused-secret"), output_path=None, declared_encoding="ascii",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


def test_active_content_page_metadata_and_signature_fields_outside_scope():
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(source=encoded_pdf(extras=True), output_path=None))
    assert result.outcome == "changed"
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert str(actual.Root.OpenAction.S) == "/JavaScript"
        assert str(actual.Root.AcroForm.Fields[0].FT) == "/Sig"
        assert actual.pages[0].obj.Metadata.read_bytes() == b"Keep page-level metadata"
        assert "/Info" not in actual.trailer
        assert "/Metadata" not in actual.Root


@pytest.mark.parametrize("data", [b"", b"not a PDF", b"%PDF-1.7\ninvalid"])
def test_malformed_input_returns_no_artifact(data, tmp_path):
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output.pdf"))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.issues[0].code == PdfMetadataFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_strict_open_does_not_repair_cross_reference():
    data = encoded_pdf()
    prefix, _ = data.rsplit(b"startxref\n", 1)
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(
        source=prefix + b"startxref\n0\n%%EOF\n", output_path=None,
    ))
    assert result.report.issues[0].code == PdfMetadataFinding.DECODE_FAILED
    assert result.artifact is None


def test_password_required_pdf_must_use_access_worker_first(tmp_path):
    data = encoded_pdf(user="open-me")
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(
        source=Artifact(data=data, password="open-me"), output_path=tmp_path / "output.pdf",   # ggignore: synthetic PDF fixture password.
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == PdfMetadataFinding.PASSWORD_REQUIRED
    assert "open-me" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())
    accessible = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=Artifact(data=data, password="open-me"), output_path=None,   # ggignore: synthetic PDF fixture password.
    ))
    sanitized = PdfMetadataSanitizer().normalize(NormalizationRequest(source=accessible.artifact, output_path=None))
    assert sanitized.outcome == "changed"


def test_empty_password_encrypted_pdf_can_be_read():
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(source=encoded_pdf(user=""), output_path=None))
    assert result.outcome == "changed"
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert not actual.is_encrypted
        assert "/Info" not in actual.trailer
        assert "/Metadata" not in actual.Root


@pytest.mark.parametrize("error", [pikepdf.PdfError("private encoder diagnostics"), ValueError("private error"), OSError("disk full")])
def test_save_failure_cleanup(error, monkeypatch, tmp_path):
    data = encoded_pdf()

    def broken_save(self, output, **kwargs):
        assert kwargs["encryption"] is False
        assert kwargs["fix_metadata_version"] is False
        output.write(b"partial output")
        raise error

    monkeypatch.setattr(pikepdf.Pdf, "save", broken_save)
    request = NormalizationRequest(source=data, output_path=tmp_path / "output.pdf")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            PdfMetadataSanitizer().normalize(request)
        assert caught.value is error
    else:
        result = PdfMetadataSanitizer().normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == PdfMetadataFinding.ENCODE_FAILED
        assert not result.report.document_info_removed
        assert not result.report.xmp_removed
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_removal_error_returns_safe_failure(monkeypatch, tmp_path):
    class BrokenDocument:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def trailer(self):
            raise pikepdf.PdfError("private metadata diagnostics")

    monkeypatch.setattr(pikepdf.Pdf, "open", lambda *args, **kwargs: BrokenDocument())
    result = PdfMetadataSanitizer().normalize(NormalizationRequest(source=b"test fixture", output_path=tmp_path / "output.pdf"))
    assert result.artifact is None
    assert result.report.issues[0].code == PdfMetadataFinding.SANITIZATION_FAILED
    assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_existing_destination_never_overwritten(tmp_path):
    path = tmp_path / "existing.pdf"
    data = encoded_pdf()
    path.write_bytes(data)
    with pytest.raises(FileExistsError):
        PdfMetadataSanitizer().normalize(NormalizationRequest(source=path, output_path=path))
    assert path.read_bytes() == data


def test_worker_contract():
    worker = PdfMetadataSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_pdf_metadata"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "changed", "page_count": 2},
    {"outcome": "no_change", "page_count": 2, "document_info_removed": True},
    {"outcome": "no_change", "page_count": 2, "xmp_removed": True},
    {"outcome": "no_change"}, {"outcome": "no_change", "page_count": -1},
    {"outcome": "changed", "xmp_removed": True},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        PdfMetadataReport(**kwargs)


def test_report_read_only_and_serializable():
    report = PdfMetadataReport(outcome="changed", document_info_removed=True, page_count=2)
    assert PdfMetadataReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.xmp_removed = True
    with pytest.raises(ValidationError):
        PdfMetadataIssue(code=PdfMetadataFinding.DECODE_FAILED, message=" ")
