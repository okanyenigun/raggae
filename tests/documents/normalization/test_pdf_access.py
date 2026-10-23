"""Real encrypted PDFs exercise the shared contract and failure handling."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from io import BytesIO

import pikepdf
import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    PdfAccessFinding,
    PdfAccessIssue,
    NormalizationWorker,
    PdfAccessReport,
    PdfDecryptionNormalizer,
    StrictTextEncodingNormalizer,
)


def encoded_pdf(*, pages=2, user=None):
    with pikepdf.Pdf.new() as document, BytesIO() as output:
        for _ in range(pages):
            page = document.add_blank_page(page_size=(240, 360))
            page.obj.Contents = document.make_stream(b"q 1 0 0 rg 10 10 20 20 re f Q")
        document.docinfo["/Title"] = "Keep this title"
        encryption = None if user is None else pikepdf.Encryption(user=user, owner="owner-secret")
        document.save(output, encryption=encryption)
        return output.getvalue()


@pytest.mark.parametrize("password", ["open-me", b"open-me", "owner-secret"])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_unlocks_to_same_content_without_password(password, source_kind, disk, tmp_path):
    original = encoded_pdf(pages=3, user="open-me")
    source = original
    if source_kind == "path":
        path = tmp_path / "source.bin"  # The filename does not select the format.
        path.write_bytes(original)
        source = str(path)
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "unlocked.pdf" if disk else None
    request = NormalizationRequest(source=source, output_path=destination, password=password)
    result = PdfDecryptionNormalizer().normalize(request)
    assert type(result) is NormalizationResult
    assert isinstance(result.report, PdfAccessReport)
    assert result.outcome == "changed"
    assert result.report.source_encrypted is True
    assert result.report.password_required is True
    assert result.report.page_count == 3
    assert result.report.issues == ()
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes()), attempt_recovery=False) as actual:
        assert not actual.is_encrypted
        assert len(actual.pages) == 3
        assert actual.docinfo["/Title"] == "Keep this title"
        for page in actual.pages:
            assert list(page.obj.MediaBox) == [0, 0, 240, 360]
            assert page.obj.Contents.read_bytes() == b"q 1 0 0 rg 10 10 20 20 re f Q"
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert PdfAccessReport.model_validate_json(result.report.model_dump_json()) == result.report
    assert "open-me" not in result.report.model_dump_json()
    assert "owner-secret" not in repr(result)


@pytest.mark.parametrize("user", [None, ""])
@pytest.mark.parametrize("disk", [False, True])
def test_accessible_pdf_returns_original(user, disk, tmp_path):
    original = Artifact(data=encoded_pdf(user=user))
    result = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=original, output_path=tmp_path / "unused.pdf" if disk else None,
        password="ignored-secret",
    ))
    assert result.outcome == "no_change"
    assert result.artifact is original
    assert result.report.source_encrypted is (user is not None)
    assert result.report.password_required is False
    assert result.report.page_count == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("password", [None, "", b"", "wrong-secret", b"wrong-secret"])
@pytest.mark.parametrize("disk", [False, True])
def test_missing_and_wrong_passwords(password, disk, tmp_path):
    request = NormalizationRequest(
        source=encoded_pdf(user="open-me"), password=password,
        output_path=tmp_path / "output.pdf" if disk else None,
    )
    result = PdfDecryptionNormalizer().normalize(request)
    assert result.outcome == "failed"
    assert result.artifact is None
    expected = PdfAccessFinding.PASSWORD_REQUIRED if password in (None, "", b"") else PdfAccessFinding.DECRYPT_FAILED
    assert result.report.issues[0].code == expected
    assert result.report.source_encrypted is True
    assert result.report.password_required is True
    assert "wrong-secret" not in repr(request)
    assert "wrong-secret" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("data", [b"", b"not a PDF", b"%PDF-1.7\ninvalid"])
def test_malformed_input_returns_no_output(data, tmp_path):
    result = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=data, output_path=tmp_path / "output.pdf", password="secret",
    ))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.issues[0].code == PdfAccessFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_strict_open_does_not_recover_bad_cross_reference(tmp_path):
    data = encoded_pdf()
    prefix, _ = data.rsplit(b"startxref\n", 1)
    broken = prefix + b"startxref\n0\n%%EOF\n"
    result = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=broken, output_path=tmp_path / "output.pdf",
    ))
    assert result.report.issues[0].code == PdfAccessFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error", [
    pikepdf.PdfError("private encoder diagnostics"), ValueError("private encoder configuration"),
    OSError("storage full"),
])
def test_failed_save_discards_partial_output(error, monkeypatch, tmp_path):
    data = encoded_pdf(user="open-me")

    def broken_save(self, output, **kwargs):
        assert kwargs["encryption"] is False
        output.write(b"partial PDF")
        raise error

    monkeypatch.setattr(pikepdf.Pdf, "save", broken_save)
    request = NormalizationRequest(
        source=data, output_path=tmp_path / "output.pdf", password="open-me",
    )
    worker = PdfDecryptionNormalizer()
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            worker.normalize(request)
        assert caught.value is error
    else:
        result = worker.normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == PdfAccessFinding.ENCODE_FAILED
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("same_source", [False, True])
def test_never_overwrites_existing_file(same_source, tmp_path):
    original = encoded_pdf(user="open-me")
    path = tmp_path / "existing.pdf"
    path.write_bytes(original)
    request = NormalizationRequest(
        source=path if same_source else original, output_path=path, password="open-me",
    )
    with pytest.raises(FileExistsError):
        PdfDecryptionNormalizer().normalize(request)
    assert path.read_bytes() == original


@pytest.mark.parametrize("password", [123, True, bytearray(b"secret"), object()])
def test_invalid_password_type_rejected_before_io(password):
    with pytest.raises(TypeError, match="password"):
        NormalizationRequest(source="does-not-exist.pdf", output_path=None, password=password)


@pytest.mark.parametrize("password", [" secret with spaces ", b" secret with spaces "])
def test_request_hides_password_and_preserves_exact_value(password):
    request = NormalizationRequest(source=b"data", output_path=None, password=password)
    assert request.password == password
    assert "secret" not in repr(request)
    assert "password=" not in repr(request)
    assert replace(request, source=Artifact(data=b"new")).password == password
    with pytest.raises(FrozenInstanceError):
        request.password = "changed"


def test_other_worker_ignores_password():
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=b"hello", output_path=None, password="unused-secret",
    ))
    assert result.outcome == "no_change"
    assert "unused-secret" not in repr(result)


def test_pdf_worker_ignores_declared_encoding():
    result = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=encoded_pdf(), output_path=None, declared_encoding="ascii",
    ))
    assert result.outcome == "no_change"


def test_worker_contract_and_no_secret_stored_on_worker():
    worker = PdfDecryptionNormalizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_pdf_decryption"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})
    assert set(vars(worker)) == {"_io_handler"}


def test_returned_artifact_can_feed_next_call():
    worker = PdfDecryptionNormalizer()
    first = worker.normalize(NormalizationRequest(
        source=encoded_pdf(user="open-me"), output_path=None, password="open-me",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact


def test_independent_calls_share_no_password_state():
    worker = PdfDecryptionNormalizer()
    data = encoded_pdf(user="open-me")

    def run(password):
        return worker.normalize(NormalizationRequest(source=data, output_path=None, password=password)).outcome

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, ["open-me", "wrong", None] * 3))
    assert results == ["changed", "failed", "failed"] * 3


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"},
    {"outcome": "changed", "source_encrypted": False, "password_required": False, "page_count": 1},
    {"outcome": "no_change", "source_encrypted": True, "password_required": True, "page_count": 1},
    {"outcome": "no_change", "source_encrypted": False, "password_required": False},
    {"outcome": "no_change", "source_encrypted": False, "password_required": False, "page_count": -1},
    {"outcome": "no_change", "source_encrypted": False, "password_required": False, "page_count": True},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        PdfAccessReport(**kwargs)


def test_report_and_issue_are_read_only_and_serializable():
    issue = PdfAccessIssue(code=PdfAccessFinding.PASSWORD_REQUIRED, message="Password required.")
    report = PdfAccessReport(outcome="failed", issues=(issue,))
    assert PdfAccessReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.outcome = "changed"
    with pytest.raises(ValidationError):
        issue.message = "new"
    with pytest.raises(ValidationError):
        PdfAccessIssue(code=PdfAccessFinding.DECODE_FAILED, message=" ")
