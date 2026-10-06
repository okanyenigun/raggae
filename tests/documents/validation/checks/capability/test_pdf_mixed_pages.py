import pikepdf
from PIL import Image
from pypdf import PdfReader
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("nesting", [0, 1, 2])
@pytest.mark.parametrize("budget,characters,qualifying,empty", [(1, 0, 0, True), (2, 0, 0, False),
                                                             (3, 25, 1, False), (4, 25, 1, False)])
def test_real_blank_scan_text_sequence_respects_sample_window(
    tmp_path, pdf_factory, scanned_pdf_factory, capability_pdf_factory, monkeypatch,
    forbidden_operation, nesting, budget, characters, qualifying, empty,
):
    blank = pdf_factory("blank.pdf")
    scan = scanned_pdf_factory(nesting=nesting)
    text = capability_pdf_factory("text.pdf")
    path = tmp_path / "mixed.pdf"
    with pikepdf.Pdf.new() as combined:
        for source in (blank, scan, text):
            with pikepdf.Pdf.open(source) as document:
                combined.pages.append(document.pages[0])
        combined.save(path)
    reader = PdfReader(path)
    assert [(page.extract_text() or "").strip() for page in reader.pages] == ["", "", "A" * 25]
    with pikepdf.Pdf.open(path) as document:
        assert [len(page.get_images()) for page in document.pages] == [0, 1, 0]
    before = path.read_bytes()
    monkeypatch.setattr(Image, "open", forbidden_operation)
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=budget)).check(path)
    assert result.codes == ((Code.EMPTY_DOCUMENT,) if empty else ())
    assert dict(result.facts) == {
        "has_extractable_text": qualifying > 0,
        "pages_sampled": min(budget, 3),
        "pages_with_text": qualifying,
        "characters_sampled": characters,
    }
    assert path.read_bytes() == before


@pytest.mark.parametrize("nesting", [0, 1, 2])
@pytest.mark.parametrize("text", ["A", "A" * 25])
def test_real_scan_with_text_on_same_page(scanned_pdf_factory, monkeypatch, forbidden_operation, nesting, text):
    path = scanned_pdf_factory(nesting=nesting)
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as document:
        page = document.pages[0]
        font = document.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
        page.obj.Resources.Font = pikepdf.Dictionary(F1=font)
        text_stream = document.make_stream(f"BT /F1 10 Tf 10 40 Td ({text}) Tj ET".encode("ascii"))
        page.obj.Contents = pikepdf.Array([page.obj.Contents, text_stream])
        document.save(path)
    assert PdfReader(path).pages[0].extract_text().strip() == text
    with pikepdf.Pdf.open(path) as document:
        assert len(document.pages[0].get_images()) == 1
    before = path.read_bytes()
    monkeypatch.setattr(Image, "open", forbidden_operation)
    result = PdfCapabilityProbe().check(path)
    qualifying = int(len(text) >= 20)
    assert result.codes == ()
    assert dict(result.facts) == {
        "has_extractable_text": qualifying > 0,
        "pages_sampled": 1,
        "pages_with_text": qualifying,
        "characters_sampled": len(text),
    }
    assert path.read_bytes() == before
