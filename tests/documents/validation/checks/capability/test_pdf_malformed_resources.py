from types import SimpleNamespace

import pikepdf
from pypdf import PdfReader
from pypdf.generic import NumberObject
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe
from raggae.documents.validation.checks.capability import pdf as pdf_module
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


@pytest.mark.parametrize("location", ["xobjects", "resources"])
def test_real_malformed_resource_dictionary_is_unreadable_not_empty(pdf_factory, location):
    path = pdf_factory()
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as document:
        document.pages[0].obj.Resources = (
            pikepdf.Dictionary(XObject=7) if location == "xobjects" else 7)
        document.save(path)
    # The installed PDF backend accepts the page, but preserves invalid metadata.
    reader = PdfReader(path)
    assert len(reader.pages) == 1
    resources = reader.pages[0].raw_get("/Resources").get_object()
    broken = resources.raw_get("/XObject") if location == "xobjects" else resources
    assert isinstance(broken, NumberObject)
    assert int(broken) == 7
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(path, "pdf")
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": path.name}
    assert dict(result.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("error_type", [OSError, ValueError])
@pytest.mark.parametrize("floor", [0, 20])
def test_failed_resource_inspection_is_not_proof_of_empty_document(monkeypatch, missing_path, error_type, floor):
    class BrokenResources:
        def get(self, key):
            raise error_type("cannot inspect image resources")

    page = SimpleNamespace(extract_text=lambda: "", get=lambda key: BrokenResources())
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=floor)).check(missing_path, "pdf")
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": missing_path.name}
    assert dict(result.facts) == {}
