import pikepdf
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import IndirectObject
import pytest

from raggae.documents.validation import PdfCapabilityProbe


@pytest.mark.parametrize("indirect_resources,indirect_xobjects", [(False, False), (True, False),
                                                               (False, True), (True, True)])
@pytest.mark.parametrize("nesting", [0, 1, 2])
def test_real_scan_is_not_empty_even_with_form_wrapped_images(
    scanned_pdf_factory, monkeypatch, forbidden_operation, nesting, indirect_resources, indirect_xobjects,
):
    path = scanned_pdf_factory(nesting=nesting, indirect_resources=indirect_resources,
                               indirect_xobjects=indirect_xobjects)
    before = path.read_bytes()
    # Establish the real image and absence of a text layer independently.
    with pikepdf.Pdf.open(path) as document:
        images = tuple(document.pages[0].get_images().values())
        assert len(images) == 1
        assert (int(images[0].Width), int(images[0].Height)) == (1, 1)
    reader = PdfReader(path)
    page = reader.pages[0]
    assert (page.extract_text() or "").strip() == ""
    resources = page.raw_get("/Resources")
    assert isinstance(resources, IndirectObject) is indirect_resources
    xobjects = resources.get_object().raw_get("/XObject")
    assert isinstance(xobjects, IndirectObject) is indirect_xobjects
    top = next(iter(xobjects.get_object().values())).get_object()
    assert str(top["/Subtype"]) == ("/Form" if nesting else "/Image")
    monkeypatch.setattr(Image, "open", forbidden_operation)
    monkeypatch.setattr(pikepdf.PdfImage, "as_pil_image", forbidden_operation)
    monkeypatch.setattr(pikepdf.PdfImage, "extract_to", forbidden_operation)

    result = PdfCapabilityProbe().check(path, "pdf")
    assert result.codes == ()
    assert dict(result.facts) == {
        "has_extractable_text": False,
        "pages_sampled": 1,
        "pages_with_text": 0,
        "characters_sampled": 0,
    }
    assert path.read_bytes() == before
