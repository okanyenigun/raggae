from types import SimpleNamespace

from pypdf.errors import PdfReadError
from pypdf.generic import IndirectObject, NullObject
import pytest

from raggae.documents.validation import PdfCapabilityProbe
from raggae.documents.validation.checks.capability import pdf as pdf_module
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


LOCATIONS = ["resources", "xobjects", "form_resources", "form_xobjects"]


def page_with_value(location, value):
    if location == "resources":
        resources = value
    elif location == "xobjects":
        resources = {"/XObject": value}
    else:
        form_resources = value if location == "form_resources" else {"/XObject": value}
        resources = {"/XObject": {"/Fm": {"/Subtype": "/Form", "/Resources": form_resources}}}
    return SimpleNamespace(extract_text=lambda: "", get=lambda key: resources)


def run_pages(monkeypatch, path, pages):
    monkeypatch.setattr(pdf_module, "PdfReader", lambda target: SimpleNamespace(pages=pages, is_encrypted=False))
    return PdfCapabilityProbe().check(path, "pdf")


def assert_unreadable(result, path):
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": path.name}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("location", LOCATIONS)
@pytest.mark.parametrize("value", [0, False, 7, "broken", [], ()])
def test_present_non_dictionary_metadata_is_not_treated_as_absent(monkeypatch, missing_path, location, value):
    assert_unreadable(run_pages(monkeypatch, missing_path, [page_with_value(location, value)]), missing_path)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("value", [None, NullObject(), 0, False, "broken", []])
def test_invalid_xobject_entry_is_unreadable(monkeypatch, missing_path, nested, value):
    location = "form_xobjects" if nested else "xobjects"
    page = page_with_value(location, {"/Broken": value})
    assert_unreadable(run_pages(monkeypatch, missing_path, [page]), missing_path)


@pytest.mark.parametrize("location", LOCATIONS)
@pytest.mark.parametrize("value", [None, NullObject(), {}])
def test_optional_null_or_empty_dictionaries_still_mean_no_images(monkeypatch, missing_path, location, value):
    result = run_pages(monkeypatch, missing_path, [page_with_value(location, value)])
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.findings[0].detail) == {"pages_sampled": 1}
    assert dict(result.facts) == {
        "has_extractable_text": False, "pages_sampled": 1,
        "pages_with_text": 0, "characters_sampled": 0,
    }


@pytest.mark.parametrize("phase", ["page", "resources", "xobjects", "reference"])
@pytest.mark.parametrize("error_type", [OSError, ValueError, PdfReadError])
def test_known_inspection_errors_are_translated_at_each_boundary(monkeypatch, missing_path, phase, error_type):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise error_type("cannot inspect resources")

    class BrokenResources(dict):
        get = fail

    class BrokenXObjects(dict):
        values = fail

    if phase == "page":
        page = SimpleNamespace(extract_text=lambda: "", get=fail)
    elif phase == "resources":
        page = page_with_value("resources", BrokenResources())
    elif phase == "xobjects":
        page = page_with_value("xobjects", BrokenXObjects())
    else:
        reference = IndirectObject(1, 0, SimpleNamespace(get_object=fail))
        page = page_with_value("resources", reference)
    assert_unreadable(run_pages(monkeypatch, missing_path, [page]), missing_path)
    assert calls == [True]


@pytest.mark.parametrize("error_type", [RuntimeError, AssertionError])
def test_unrelated_errors_are_not_silently_reclassified(monkeypatch, missing_path, error_type):
    class BrokenResources(dict):
        def get(self, key):
            raise error_type("unrelated worker error")

    with pytest.raises(error_type, match="unrelated worker error"):
        run_pages(monkeypatch, missing_path, [page_with_value("resources", BrokenResources())])


def test_resource_failure_discards_previously_accumulated_text_facts(monkeypatch, missing_path):
    good = SimpleNamespace(extract_text=lambda: "A" * 25, get=lambda key: {})
    bad = page_with_value("form_xobjects", 7)
    assert_unreadable(run_pages(monkeypatch, missing_path, [good, bad]), missing_path)


def test_unresolved_indirect_reference_is_not_missing_metadata(monkeypatch, missing_path):
    reference = IndirectObject(1, 0, SimpleNamespace(get_object=lambda reference: None))
    assert_unreadable(run_pages(monkeypatch, missing_path, [page_with_value("resources", reference)]), missing_path)


def test_failure_does_not_leak_to_next_inspection(monkeypatch, missing_path):
    pages = [page_with_value("resources", 7)]
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=pages, is_encrypted=False))
    probe = PdfCapabilityProbe()
    assert_unreadable(probe.check(missing_path), missing_path)
    pages[:] = [SimpleNamespace(extract_text=lambda: "A" * 25, get=lambda key: {})]
    result = probe.check(missing_path)
    assert result.codes == ()
    assert dict(result.facts) == {
        "has_extractable_text": True, "pages_sampled": 1,
        "pages_with_text": 1, "characters_sampled": 25,
    }
