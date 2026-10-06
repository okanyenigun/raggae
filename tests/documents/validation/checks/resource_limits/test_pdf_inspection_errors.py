from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.validation import PdfResourceProbe
from raggae.documents.validation.checks.resource_limits import pdf as pdf_module
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code


class InspectionDocument:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


@pytest.mark.parametrize("error", [OSError("image metadata could not be read"),
                                 pikepdf.PdfError("invalid image resource dictionary")],
                         ids=["read-error", "invalid-resource"])
def test_image_enumeration_failure_is_not_a_clean_result(monkeypatch, missing_path, error):
    calls = []

    def get_images():
        calls.append("image metadata")
        raise error

    page = SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=get_images)
    document = InspectionDocument(page)
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfResourceProbe().check(missing_path)
    assert calls == ["image metadata"]
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {"page_count": 1}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}


def test_invalid_image_dimensions_return_unreadable_instead_of_crashing(monkeypatch, missing_path):
    image = SimpleNamespace(Width="not an integer", Height=2)
    page = SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=lambda: {"/Im": image})
    document = InspectionDocument(page)
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    try:
        outcome = PdfResourceProbe().check(missing_path)
    finally:
        assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {"page_count": 1}


@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError,
                                      pikepdf.PdfError, TypeError, ValueError])
@pytest.mark.parametrize("stage", ["enumeration", "dimensions"])
def test_metadata_errors_return_unreadable_and_close(monkeypatch, missing_path, error_type, stage):
    def fail():
        raise error_type("metadata inspection failed")

    class BrokenImage:
        @property
        def Width(self):
            fail()

    page = SimpleNamespace(mediabox=(0, 0, 72, 72),
                           get_images=fail if stage == "enumeration" else lambda: {"/Im": BrokenImage()})
    document = InspectionDocument(page)
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfResourceProbe().check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {"page_count": 1}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}
    assert "metadata inspection failed" in outcome.findings[0].message


@pytest.mark.parametrize("error_type", [RuntimeError, AssertionError, AttributeError])
def test_unexpected_programming_error_still_propagates_and_closes(monkeypatch, missing_path, error_type):
    def get_images():
        raise error_type("implementation bug")

    document = InspectionDocument(SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=get_images))
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    with pytest.raises(error_type, match="implementation bug"):
        PdfResourceProbe().check(missing_path)
    assert document.closed


def test_partial_image_enumeration_is_not_a_success(monkeypatch, missing_path):
    def image_pairs():
        yield "/Im1", SimpleNamespace(Width=1, Height=1)
        raise pikepdf.PdfError("later image could not be inspected")

    document = InspectionDocument(SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=image_pairs))
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfResourceProbe().check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {"page_count": 1}


@pytest.mark.parametrize("width,height", [("invalid", 2), (2, "invalid"), ({"invalid": 1}, 2), (2, ["invalid"])])
def test_invalid_nonempty_dimensions_are_unreadable(monkeypatch, missing_path, width, height):
    image = SimpleNamespace(Width=width, Height=height)
    document = InspectionDocument(SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=lambda: {"/Im": image}))
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfResourceProbe().check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {"page_count": 1}
