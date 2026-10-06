import pikepdf
from pypdf import PdfReader
import pytest

from raggae.documents.validation import (
    CapabilityPolicy, Decision, DecisionPolicy, PdfCapabilityProbe, Severity,
)
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("format_name", [None, "pdf", " .PDF "])
@pytest.mark.parametrize("as_string", [False, True])
def test_zero_pages_path_and_format_alternatives(pdf_factory, format_name, as_string):
    path = pdf_factory(pages=0)
    assert len(PdfReader(path).pages) == 0
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(str(path) if as_string else path, format_name)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.findings[0].detail) == {"pages_sampled": 0}
    assert dict(result.facts) == {
        "has_extractable_text": False, "pages_sampled": 0,
        "pages_with_text": 0, "characters_sampled": 0,
    }
    assert path.read_bytes() == before


@pytest.mark.parametrize("user,password,opens", [
    (None, None, True), ("reader", None, False), ("reader", "", False),
    ("reader", "wrong", False), ("reader", "reader", True), ("reader", "owner", True),
    ("", None, True), ("", "owner", True),
])
def test_zero_page_encryption_keeps_open_failure_distinct_from_empty(pdf_factory, user, password, opens):
    encryption = None if user is None else pikepdf.Encryption(owner="owner", user=user, R=6)
    path = pdf_factory(pages=0, encryption=encryption)
    with pikepdf.Pdf.open(path, password=user or "") as document:
        assert len(document.pages) == 0
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(path, "pdf", password)
    assert result.codes == ((Code.EMPTY_DOCUMENT,) if opens else (Code.UNREADABLE,))
    if opens:
        assert dict(result.findings[0].detail) == {"pages_sampled": 0}
        assert dict(result.facts) == {
            "has_extractable_text": False, "pages_sampled": 0,
            "pages_with_text": 0, "characters_sampled": 0,
        }
    else:
        assert dict(result.findings[0].detail) == {"path": path.name}
        assert dict(result.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("worker_severity", [Severity.WARNING, Severity.REJECT])
def test_empty_document_remains_unweighted_and_warning_by_default(pdf_factory, worker_severity):
    path = pdf_factory(pages=0)
    probe = PdfCapabilityProbe(CapabilityPolicy(severities={Code.EMPTY_DOCUMENT: worker_severity}))
    result = probe.check(path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    default = DecisionPolicy()
    assert default.severity_of(result.findings[0]) is Severity.WARNING
    assert default.decide(result.findings) is Decision.ACCEPT_WITH_WARNINGS
    strict = DecisionPolicy(severity_by_code={Code.EMPTY_DOCUMENT: Severity.REJECT})
    assert strict.decide(result.findings) is Decision.REJECT


def test_zero_page_outcome_does_not_leak_to_later_files(pdf_factory, capability_pdf_factory):
    zero = pdf_factory("zero.pdf", pages=0)
    text = capability_pdf_factory()
    blank = pdf_factory("blank.pdf", pages=1)
    probe = PdfCapabilityProbe()
    assert probe.check(zero).findings[0].detail["pages_sampled"] == 0
    result = probe.check(text)
    assert result.codes == ()
    assert result.facts["has_extractable_text"] is True
    assert result.facts["pages_sampled"] == 1
    assert probe.check(blank).findings[0].detail["pages_sampled"] == 1
    assert probe.check(zero).findings[0].detail["pages_sampled"] == 0
