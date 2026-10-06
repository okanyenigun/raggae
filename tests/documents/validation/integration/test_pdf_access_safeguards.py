import pikepdf
import pytest

from raggae.documents.validation import (
    Decision, DecisionPolicy, DocumentInspector, EncryptionPolicy, PdfEncryptionProbe,
)


REASON = "PDF access blocked: valid password required"


@pytest.mark.parametrize("password", [None, "", "wrong-password"])
def test_real_locked_pdf_never_calls_dependent_checks_in_collection(
    integration_pdf, monkeypatch, forbidden_operation, password,
):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    before = path.read_bytes()
    inspector = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False))
    selected = (inspector._resource_probes[0], inspector._active_content_probes[0], inspector._capability_probes)
    for worker in selected:
        monkeypatch.setattr(worker, "check", forbidden_operation)
    report = inspector.validate(path, password=password)
    assert report.decision is Decision.NEEDS_PASSWORD
    assert report.checks_that_did_not == tuple(worker.name for worker in selected)
    assert all(check.skipped_because == REASON for check in report.checks[4:])
    assert path.read_bytes() == before


def test_real_locked_pdf_retries_do_not_leak_access_block(integration_pdf):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    before = path.read_bytes()
    inspector = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False))
    reports = []
    for password in (None, "wrong-password", "reader-secret", "owner-secret", None, "reader-secret"):
        report = inspector.validate(path, password=password)
        unlocked = password in {"reader-secret", "owner-secret"}
        assert report.decision is (Decision.ACCEPT if unlocked else Decision.NEEDS_PASSWORD)
        assert report.facts["password_required"] is True
        assert len(report.checks_that_ran) == (7 if unlocked else 4)
        if unlocked:
            assert report.facts["has_extractable_text"] is True
        else:
            assert "has_extractable_text" not in report.facts
        assert "reader-secret" not in report.model_dump_json()
        assert "owner-secret" not in report.model_dump_json()
        reports.append(report)
    assert reports[0].decision is Decision.NEEDS_PASSWORD
    assert reports[2].decision is Decision.ACCEPT
    assert path.read_bytes() == before


def test_empty_user_password_pdf_is_accessible_without_supplied_password(integration_pdf):
    path = integration_pdf(encryption=pikepdf.Encryption(user="", owner="owner-secret", R=6))
    report = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False)).validate(path)
    assert report.decision is Decision.ACCEPT
    assert report.facts["encrypted"] is True
    assert report.facts["password_required"] is False
    assert report.facts["has_extractable_text"] is True
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("submitted,expected,filename_code", [
    ("../upload.pdf", Decision.NEEDS_PASSWORD, "filename.path_components"),
    ("payload.exe.pdf", Decision.REJECT, "filename.dangerous_extension"),
])
def test_password_block_does_not_discard_independent_filename_findings(
    integration_pdf, submitted, expected, filename_code,
):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    report = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False)).validate(path, submitted_filename=submitted)
    assert report.decision is expected
    assert tuple(weighted.finding.code for weighted in report.findings) == (filename_code, "encryption.password_required")
    assert len(report.checks_that_ran) == 4
    assert all(check.skipped_because == REASON for check in report.checks[4:])


def test_correct_password_allows_inspection_despite_password_policy_rejection(integration_pdf):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    report = DocumentInspector(
        policy=DecisionPolicy(stop_on_first_rejection=False),
        encryption_probe=PdfEncryptionProbe(EncryptionPolicy(accept_password_protected=False)),
    ).validate(path, password="reader-secret")
    assert report.decision is Decision.REJECT
    assert tuple(weighted.finding.code for weighted in report.findings) == ("encryption.password_protected",)
    assert len(report.checks_that_ran) == 7
    assert report.facts["has_extractable_text"] is True


def test_damaged_pdf_unreadable_findings_are_not_masked_in_collection(file_factory):
    path = file_factory("damaged.pdf", b"%PDF-1.7\nnot a valid PDF structure\n")
    report = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False)).validate(path)
    assert report.decision is Decision.REJECT
    assert tuple(weighted.finding.code for weighted in report.findings) == (
        "encryption.unreadable", "resource_limits.unreadable",
        "active_content.unreadable", "capability.unreadable",
    )
    assert len(report.checks_that_ran) == 7
