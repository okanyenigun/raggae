from types import SimpleNamespace

from pypdf import PdfReader
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe
from raggae.documents.validation.checks.capability import pdf as pdf_module
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("floor", [0, 1, 2])
@pytest.mark.parametrize("text,characters", [(None, 0), (" \t\n", 0), ("A", 1), ("AB", 2)])
def test_zero_and_neighboring_floors_do_not_invent_text(monkeypatch, missing_path, floor, text, characters):
    page = SimpleNamespace(extract_text=lambda: text, get=lambda key: {})
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=floor)).check(missing_path)
    # Explicit boundary table: a minimum of zero or one accepts one character.
    qualifying = characters in ({1, 2} if floor in {0, 1} else {2})
    assert result.codes == (() if qualifying else (Code.EMPTY_DOCUMENT,))
    assert dict(result.facts) == {
        "has_extractable_text": qualifying,
        "pages_sampled": 1,
        "pages_with_text": int(qualifying),
        "characters_sampled": characters,
    }


def test_extraction_error_does_not_become_text_at_zero_floor(monkeypatch, missing_path):
    def fail():
        raise OSError("cannot extract page text")

    page = SimpleNamespace(extract_text=fail, get=lambda key: {})
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=0)).check(missing_path)
    # Preserve the existing degraded-extraction contract; this is not an open error.
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.facts) == {
        "has_extractable_text": False, "pages_sampled": 1,
        "pages_with_text": 0, "characters_sampled": 0,
    }


@pytest.mark.parametrize("nesting", [0, 1, 2])
def test_real_scan_still_needs_ocr_at_zero_floor(scanned_pdf_factory, nesting):
    path = scanned_pdf_factory(nesting=nesting)
    assert (PdfReader(path).pages[0].extract_text() or "").strip() == ""
    before = path.read_bytes()
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=0)).check(path)
    assert result.codes == ()
    assert dict(result.facts) == {
        "has_extractable_text": False, "pages_sampled": 1,
        "pages_with_text": 0, "characters_sampled": 0,
    }
    assert path.read_bytes() == before


@pytest.mark.parametrize("budget,sampled,qualifying", [(1, 1, 0), (2, 2, 1), (3, 3, 1), (4, 3, 1)])
def test_zero_floor_counts_only_sampled_nonempty_pages(monkeypatch, missing_path, budget, sampled, qualifying):
    pages = [SimpleNamespace(extract_text=lambda value=value: value, get=lambda key: {})
             for value in (None, "A", " \t")]
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=pages, is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=budget, min_characters_per_page=0)).check(missing_path)
    assert result.codes == (() if qualifying else (Code.EMPTY_DOCUMENT,))
    assert dict(result.facts) == {
        "has_extractable_text": qualifying > 0,
        "pages_sampled": sampled,
        "pages_with_text": qualifying,
        "characters_sampled": qualifying,
    }
