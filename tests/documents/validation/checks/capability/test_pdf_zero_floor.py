from types import SimpleNamespace

from pypdf import PdfReader
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe
from raggae.documents.validation.checks.capability import pdf as pdf_module
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("text,characters", [("", 0), (" ", 0), ("A", 1)])
def test_zero_character_floor_still_requires_actual_text(capability_pdf_factory, text, characters):
    path = capability_pdf_factory(texts=(text,))
    assert (PdfReader(path).pages[0].extract_text() or "").strip() == text.strip()
    before = path.read_bytes()
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=0)).check(path, "pdf")
    assert result.facts["has_extractable_text"] is (characters > 0)
    assert result.codes == (() if characters else (Code.EMPTY_DOCUMENT,))
    assert dict(result.facts) == {
        "has_extractable_text": characters > 0,
        "pages_sampled": 1,
        "pages_with_text": int(characters > 0),
        "characters_sampled": characters,
    }
    assert path.read_bytes() == before


def test_none_extraction_is_not_text_at_zero_floor(monkeypatch, missing_path):
    page = SimpleNamespace(extract_text=lambda: None, get=lambda key: {})
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=0)).check(missing_path, "pdf")
    assert result.facts["has_extractable_text"] is False
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.facts) == {
        "has_extractable_text": False,
        "pages_sampled": 1,
        "pages_with_text": 0,
        "characters_sampled": 0,
    }
