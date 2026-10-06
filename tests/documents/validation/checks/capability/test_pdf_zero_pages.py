import pikepdf
from pypdf import PdfReader
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("floor", [0, 20])
@pytest.mark.parametrize("budget", [1, 3])
def test_zero_page_pdf_reports_empty_document(pdf_factory, monkeypatch, forbidden_operation, floor, budget):
    path = pdf_factory(pages=0)
    with pikepdf.Pdf.open(path) as document:
        assert len(document.pages) == 0
        assert int(document.Root.Pages.Count) == 0
    assert len(PdfReader(path).pages) == 0
    before = path.read_bytes()
    monkeypatch.setattr(PdfCapabilityProbe, "_text_length", forbidden_operation)
    monkeypatch.setattr(PdfCapabilityProbe, "_has_image", forbidden_operation)
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=budget, min_characters_per_page=floor)).check(path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.findings[0].detail) == {"pages_sampled": 0}
    assert dict(result.facts) == {
        "has_extractable_text": False,
        "pages_sampled": 0,
        "pages_with_text": 0,
        "characters_sampled": 0,
    }
    assert path.read_bytes() == before
