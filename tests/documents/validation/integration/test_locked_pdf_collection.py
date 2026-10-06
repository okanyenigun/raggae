import pikepdf
import pytest

from raggae.documents.validation import Decision, DecisionPolicy, DocumentInspector


@pytest.mark.parametrize("password,expected_code", [
    pytest.param(None, "encryption.password_required", id="missing"),
    pytest.param("wrong-password", "encryption.password_incorrect", id="wrong"),
])
def test_locked_pdf_remains_recoverable_when_collecting_findings(
    integration_pdf, password, expected_code,
):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    before = path.read_bytes()
    report = DocumentInspector(policy=DecisionPolicy(stop_on_first_rejection=False)).validate(path, password=password)

    assert report.decision is Decision.NEEDS_PASSWORD
    assert tuple(weighted.finding.code for weighted in report.findings) == (expected_code,)
    assert report.facts["encrypted"] is True
    assert report.facts["password_required"] is True
    assert "page_count" not in report.facts
    assert "has_extractable_text" not in report.facts
    assert path.read_bytes() == before
