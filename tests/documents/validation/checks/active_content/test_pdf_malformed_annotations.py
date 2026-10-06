import pikepdf
import pytest
from types import SimpleNamespace
import re

from raggae.documents.validation import PdfActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code
from raggae.documents.validation.checks.active_content import pdf as pdf_module


@pytest.mark.parametrize("entry", [None, 7, "not an annotation"], ids=["null", "number", "string"])
def test_invalid_annotation_entry_returns_unreadable_instead_of_crashing(active_pdf_factory, entry):
    def customize(document):
        document.pages[0].obj.Annots = pikepdf.Array([entry])

    path = active_pdf_factory(customize=customize)
    before = path.read_bytes()
    # Strict parsing succeeds, but the annotation metadata is not a dictionary.
    with pikepdf.Pdf.open(path, attempt_recovery=False) as document:
        annotations = document.pages[0].obj.Annots
        assert len(annotations) == 1
        if entry is None:
            assert annotations[0] is None
        else:
            assert str(annotations[0]) == str(entry)
    outcome = PdfActiveContentProbe().check(path, "pdf")
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}
    assert path.read_bytes() == before


@pytest.mark.parametrize("container", [0, 7, "", "not an array", {}, {"/Subtype": "/Link"}])
def test_invalid_annotation_container_is_unreadable(monkeypatch, missing_path, container):
    # pikepdf removes malformed /Annots containers when loading real pages.
    # A controlled page preserves the value so this test actually reaches the guard.
    class Document:
        Root = {}
        pages = [{"/Annots": container}]
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfActiveContentProbe().check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}
    assert "not an array" in outcome.findings[0].message


def test_real_backend_removes_invalid_container_before_worker_reads_page(active_pdf_factory):
    def customize(document):
        document.pages[0].obj.Annots = 0

    path = active_pdf_factory(customize=customize)
    before = path.read_bytes()
    assert re.search(rb"/Annots\s+0\b", before)
    with pikepdf.Pdf.open(path, attempt_recovery=False) as document:
        page = document.pages[0]
        assert page.obj.get("/Annots") is None
        assert page.get("/Annots") is None
        assert any("Annots is not an array; removing" in warning for warning in document.get_warnings())
    # This characterizes backend normalization, not proof of original-file validity.
    assert PdfActiveContentProbe().check(path).codes == ()
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry", [False, True, [], [7]])
def test_other_non_dictionary_entries_are_unreadable(active_pdf_factory, entry):
    def customize(document):
        value = pikepdf.Array(entry) if isinstance(entry, list) else entry
        document.pages[0].obj.Annots = pikepdf.Array([value])

    path = active_pdf_factory(customize=customize)
    outcome = PdfActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert "not a dictionary" in outcome.findings[0].message


@pytest.mark.parametrize("container", [None, []])
def test_missing_or_empty_annotations_remain_clean(active_pdf_factory, container):
    def customize(document):
        if container is not None:
            document.pages[0].obj.Annots = pikepdf.Array(container)

    assert PdfActiveContentProbe().check(active_pdf_factory(customize=customize)).codes == ()


@pytest.mark.parametrize("invalid_first", [False, True])
def test_mixed_valid_invalid_entries_are_unreadable_and_later_calls_are_clean(active_pdf_factory, invalid_first):
    def customize(document):
        annotation = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                                       Rect=pikepdf.Array([0, 0, 10, 10]),
                                       A=pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program"))
        document.pages[1].obj.Annots = pikepdf.Array([None, annotation] if invalid_first else [annotation, None])

    invalid = active_pdf_factory("invalid.pdf", customize=customize)
    clean = active_pdf_factory("clean.pdf")
    probe = PdfActiveContentProbe()
    outcome = probe.check(invalid)
    assert outcome.codes == (Code.UNREADABLE,)
    assert "page 2" in outcome.findings[0].message
    assert probe.check(clean).codes == ()


def test_malformed_entry_closes_document_without_claiming_partial_success(monkeypatch, missing_path):
    class Document:
        Root = {"/OpenAction": {"/S": "/JavaScript"}}
        pages = [{"/Annots": [None]}]
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfActiveContentProbe().check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("error_type", [AttributeError, RuntimeError, AssertionError])
def test_unrelated_programming_errors_still_propagate_and_close(monkeypatch, missing_path, error_type):
    class BrokenAnnotation(dict):
        def get(self, *args, **kwargs):
            raise error_type("programming bug")

    class Document:
        Root = {}
        pages = [{"/Annots": [BrokenAnnotation()]}]
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    with pytest.raises(error_type, match="programming bug"):
        PdfActiveContentProbe().check(missing_path)
    assert document.closed
