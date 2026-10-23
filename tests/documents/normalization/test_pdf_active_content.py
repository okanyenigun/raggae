"""Inspect saved PDF structures, preservation, counts, and failure cleanup."""

from io import BytesIO
from types import SimpleNamespace

import pikepdf
import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    PdfActiveContentFinding,
    PdfActiveContentIssue,
    NormalizationWorker,
    PdfActiveContentReport,
    PdfActiveContentSanitizer,
    PdfDecryptionNormalizer,
)


def action(kind="JavaScript"):
    value = pikepdf.Dictionary(S=pikepdf.Name("/" + kind))
    if kind == "JavaScript":
        value.JS = pikepdf.String("app.alert('example')")
    elif kind == "URI":
        value.URI = pikepdf.String("https://example.invalid")
    elif kind in ("Launch", "GoToR"):
        value.F = pikepdf.String("external.pdf")
    return value


def annotation(document, subtype="Link", **fields):
    return document.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name("/" + subtype),
        Rect=pikepdf.Array([0, 0, 10, 10]), **fields,
    ))


def encoded_pdf(configure=None, *, encrypted=None):
    with pikepdf.Pdf.new() as document, BytesIO() as output:
        for _ in range(2):
            page = document.add_blank_page(page_size=(240, 360))
            page.obj.Contents = document.make_stream(b"q 1 0 0 rg 10 10 20 20 re f Q")
        document.docinfo["/Title"] = "Keep ordinary metadata"
        if configure is not None:
            configure(document)
        encryption = None if encrypted is None else pikepdf.Encryption(user=encrypted, owner="owner")
        document.save(output, encryption=encryption)
        return output.getvalue()


def add_all_features(document):
    names = pikepdf.Dictionary()
    names.JavaScript = pikepdf.Dictionary(Names=pikepdf.Array(["script", action()]))
    names.EmbeddedFiles = pikepdf.Dictionary(Names=pikepdf.Array([
        "file", pikepdf.Dictionary(Type=pikepdf.Name.Filespec, F=pikepdf.String("attached.txt")),
    ]))
    names.Dests = pikepdf.Dictionary(Names=pikepdf.Array([]))
    document.Root.Names = names
    document.Root.OpenAction = action()
    document.Root.AA = pikepdf.Dictionary(WC=action())
    for page in document.pages:
        page.obj.AA = pikepdf.Dictionary(O=action())
        page.obj.Annots = pikepdf.Array([
            annotation(document, "FileAttachment"),
            annotation(document, "RichMedia"),
            annotation(document, A=action("JavaScript"), AA=pikepdf.Dictionary(E=action())),
            annotation(document, A=action("Launch")),
            annotation(document, A=action("URI")),
            annotation(document, A=action("GoToR")),
            annotation(document, A=pikepdf.Dictionary(S=pikepdf.Name.GoTo, D=pikepdf.Array([page.obj, pikepdf.Name.Fit]))),
            annotation(document, "Text", Contents=pikepdf.String("Keep this annotation")),
        ])


@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_all_declared_removals_and_preserved_page_content(source_kind, disk, tmp_path):
    original = encoded_pdf(add_all_features)
    source = original
    if source_kind == "path":
        path = tmp_path / "source.bin"
        path.write_bytes(original)
        source = path
    elif source_kind == "artifact":
        source = Artifact(data=original)
    request = NormalizationRequest(
        source=source, output_path=tmp_path / "clean.pdf" if disk else None,
    )
    result = PdfActiveContentSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.page_count == 2
    assert result.report.removals == {
        "script": 3, "automatic_action": 6, "launch_action": 2,
        "embedded_file": 3, "rich_media": 2, "remote_reference": 4,
    }
    assert request.source.read_bytes() == original
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes()), attempt_recovery=False) as actual:
        assert not actual.is_encrypted
        assert len(actual.pages) == 2
        assert actual.docinfo["/Title"] == "Keep ordinary metadata"
        assert "/OpenAction" not in actual.Root
        assert "/AA" not in actual.Root
        assert list(actual.Root.Names.keys()) == ["/Dests"]
        for page in actual.pages:
            assert "/AA" not in page.obj
            assert list(page.obj.MediaBox) == [0, 0, 240, 360]
            assert page.obj.Contents.read_bytes() == b"q 1 0 0 rg 10 10 20 20 re f Q"
            annotations = page.obj.Annots
            assert len(annotations) == 6
            for item in list(annotations)[:4]:
                assert "/AA" not in item
                assert "/A" not in item
            assert str(annotations[4].A.S) == "/GoTo"
            assert str(annotations[5].Contents) == "Keep this annotation"
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert PdfActiveContentReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("kind,category", [
    ("JavaScript", "script"), ("Launch", "launch_action"),
    ("URI", "remote_reference"), ("GoToR", "remote_reference"),
])
def test_each_annotation_action(kind, category):
    def configure(document):
        document.pages[0].obj.Annots = pikepdf.Array([annotation(document, A=action(kind))])

    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(configure), output_path=None,
    ))
    assert result.report.removals == {category: 1}
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert "/A" not in actual.pages[0].obj.Annots[0]


def test_empty_names_dictionary_removed_after_tree_removal():
    def configure(document):
        document.Root.Names = pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(Names=pikepdf.Array([])))

    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(configure), output_path=None,
    ))
    assert result.report.removals == {"script": 1}
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert "/Names" not in actual.Root


@pytest.mark.parametrize("empty_names", [False, True])
@pytest.mark.parametrize("disk", [False, True])
def test_no_targeted_content_returns_exact_original(empty_names, disk, tmp_path):
    def configure(document):
        if empty_names:
            document.Root.Names = pikepdf.Dictionary()

    original = Artifact(data=encoded_pdf(configure))
    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=original, output_path=tmp_path / "unused.pdf" if disk else None,
    ))
    assert result.outcome == "no_change"
    assert result.artifact is original
    assert result.report.removals == {}
    assert not list(tmp_path.iterdir())


def test_internal_open_destination_is_removed_as_automatic_action():
    def configure(document):
        document.Root.OpenAction = pikepdf.Array([document.pages[0].obj, pikepdf.Name.Fit])

    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(configure), output_path=None,
    ))
    assert result.report.removals == {"automatic_action": 1}


def test_changed_output_is_idempotent():
    worker = PdfActiveContentSanitizer()
    first = worker.normalize(NormalizationRequest(source=encoded_pdf(add_all_features), output_path=None))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact


@pytest.mark.parametrize("source", [b"", b"not a PDF", b"%PDF-1.7\ninvalid"])
def test_malformed_pdf_is_decode_failure(source, tmp_path):
    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=source, output_path=tmp_path / "output.pdf",
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == PdfActiveContentFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("malformed", ["names", "annotation", "action"])
def test_malformed_targeted_structure_produces_no_output(malformed, tmp_path):
    def configure(document):
        if malformed == "names":
            document.Root.Names = pikepdf.Array([])
        elif malformed == "annotation":
            document.pages[0].obj.Annots = pikepdf.Array([pikepdf.String("invalid")])
        else:
            document.pages[0].obj.Annots = pikepdf.Array([annotation(document, A=pikepdf.Array([]))])

    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(configure), output_path=tmp_path / "output.pdf",
    ))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.issues[0].code == PdfActiveContentFinding.SANITIZATION_FAILED
    assert not list(tmp_path.iterdir())


def test_malformed_annotation_collection_at_removal_boundary():
    # Saving/reopening a PDF normalizes an invalid /Annots dictionary away.
    # Supply it directly to the removal boundary so this test exercises the guard.
    document = SimpleNamespace(
        Root=pikepdf.Dictionary(),
        pages=[SimpleNamespace(obj=pikepdf.Dictionary(Annots=pikepdf.Dictionary()))],
    )
    with pytest.raises(ValueError, match="annotation collection must be an array"):
        PdfActiveContentSanitizer._remove_active_content(document)


def test_password_is_not_used_to_duplicate_pdf_access_work(tmp_path):
    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(add_all_features, encrypted="open-me"), password="open-me",
        output_path=tmp_path / "output.pdf",
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == PdfActiveContentFinding.PASSWORD_REQUIRED
    assert "open-me" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_pdf_access_output_feeds_sanitizer():
    accessible = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=encoded_pdf(add_all_features, encrypted="open-me"), password="open-me",
        output_path=None,
    ))
    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=accessible.artifact, output_path=None,
    ))
    assert result.outcome == "changed"
    assert result.report.page_count == 2


def test_empty_password_pdf_can_be_sanitized():
    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(add_all_features, encrypted=""), output_path=None,
    ))
    assert result.outcome == "changed"
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert not actual.is_encrypted


@pytest.mark.parametrize("error", [pikepdf.PdfError("private error"), ValueError("private error"), OSError("disk full")])
def test_failed_save_cleanup(error, monkeypatch, tmp_path):
    data = encoded_pdf(add_all_features)

    def broken_save(self, output, **kwargs):
        output.write(b"partial PDF")
        raise error

    monkeypatch.setattr(pikepdf.Pdf, "save", broken_save)
    request = NormalizationRequest(source=data, output_path=tmp_path / "output.pdf")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            PdfActiveContentSanitizer().normalize(request)
        assert caught.value is error
    else:
        result = PdfActiveContentSanitizer().normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == PdfActiveContentFinding.ENCODE_FAILED
        assert result.report.removals == {}
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_existing_destination_not_overwritten(tmp_path):
    destination = tmp_path / "existing.pdf"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        PdfActiveContentSanitizer().normalize(NormalizationRequest(
            source=encoded_pdf(add_all_features), output_path=destination,
        ))
    assert destination.read_bytes() == b"keep"


def test_signature_fields_not_removed_to_hide_rewrite():
    def configure(document):
        add_all_features(document)
        document.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([
            document.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Signature"))),
        ]))

    result = PdfActiveContentSanitizer().normalize(NormalizationRequest(
        source=encoded_pdf(configure), output_path=None,
    ))
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert str(actual.Root.AcroForm.Fields[0].FT) == "/Sig"
    # Presence is not cryptographic signature validity; the worker makes no such claim.


def test_worker_contract_and_shared_settings():
    worker = PdfActiveContentSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_pdf_active_content"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})
    result = worker.normalize(NormalizationRequest(
        source=encoded_pdf(), output_path=None,
        declared_encoding="ascii", password="unused-secret",
    ))
    assert result.outcome == "no_change"
    assert "unused-secret" not in repr(result)


@pytest.mark.parametrize("removals", [{"script": 0}, {"script": -1}, {"script": True}, {"script": "1"}, {"unknown": 1}])
def test_invalid_counts_or_categories_rejected(removals):
    with pytest.raises(ValidationError):
        PdfActiveContentReport(outcome="changed", page_count=2, removals=removals)


def test_report_is_immutable_with_json_roundtrip():
    counts = {"script": 1}
    report = PdfActiveContentReport(outcome="changed", page_count=2, removals=counts)
    counts["script"] = 99
    assert report.removals == {"script": 1}
    with pytest.raises(TypeError):
        report.removals["script"] = 2
    with pytest.raises(ValidationError):
        report.page_count = 3
    assert PdfActiveContentReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "changed", "page_count": 1},
    {"outcome": "no_change", "page_count": 1, "removals": {"script": 1}},
    {"outcome": "no_change"}, {"outcome": "no_change", "page_count": -1},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        PdfActiveContentReport(**kwargs)


def test_blank_issue_message_rejected():
    with pytest.raises(ValidationError):
        PdfActiveContentIssue(code=PdfActiveContentFinding.DECODE_FAILED, message=" ")
