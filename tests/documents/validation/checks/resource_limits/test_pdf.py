from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.validation import PdfLimits, PdfResourceProbe
from raggae.documents.validation.checks.resource_limits import pdf as pdf_module
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code


class ResourceDocument:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def __enter__(self):
        assert not self.closed
        return self

    def __exit__(self, *exc):
        self.closed = True


def use_document(monkeypatch, document):
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))


@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("format_name", [None, "pdf", " .PDF "])
@pytest.mark.parametrize("as_string", [False, True])
def test_real_page_count_boundary_and_source_unchanged(pdf_factory, count, format_name, as_string):
    path = pdf_factory(pages=count)
    before = path.read_bytes()
    outcome = PdfResourceProbe(PdfLimits(max_pages=2)).check(str(path) if as_string else path, format_name)
    assert dict(outcome.facts) == {"page_count": count}
    assert outcome.codes == ((Code.TOO_MANY_PAGES,) if count > 2 else ())
    if count > 2:
        assert dict(outcome.findings[0].detail) == {"limit": 2, "observed": 3}
    assert path.read_bytes() == before


@pytest.mark.parametrize("size", [(99, 72), (100, 72), (101, 72), (72, 101)])
def test_real_page_longest_edge_boundary(pdf_factory, size):
    path = pdf_factory(page_size=size)
    outcome = PdfResourceProbe(PdfLimits(max_page_points=100)).check(path)
    assert outcome.codes == ((Code.PAGE_TOO_LARGE,) if max(size) > 100 else ())
    assert outcome.facts["page_count"] == 1
    if outcome.findings:
        assert dict(outcome.findings[0].detail) == {"limit": 100, "observed": 101, "page": 1}


@pytest.mark.parametrize("box,expected", [
    ((10, 20, 109, 92), None),
    ((10, 20, 110, 92), None),
    ((10, 20, 111, 92), 101),
    ((111, 92, 10, 20), 101),
    ((-111, -92, -10, -20), 101),
    (("0", "0", "101", "72"), 101),
    (None, None), ((), None), ((0, 0, 1), None),
    ((0, 0, 1, 2, 3), None), ((0, 0, "invalid", 72), None),
    ((0, 0, None, 72), None),
])
def test_media_box_offsets_reversed_coordinates_and_malformed_values(monkeypatch, missing_path, box, expected):
    # A controlled page isolates metadata shape handling; real-page boundaries are above.
    page = SimpleNamespace(mediabox=box, get_images=lambda: {})
    document = ResourceDocument([page])
    use_document(monkeypatch, document)
    outcome = PdfResourceProbe(PdfLimits(max_page_points=100)).check(missing_path)
    assert outcome.codes == (() if expected is None else (Code.PAGE_TOO_LARGE,))
    if expected is not None:
        assert dict(outcome.findings[0].detail) == {"limit": 100, "observed": expected, "page": 1}
    assert document.closed


def test_page_count_refuses_before_iteration_or_image_inspection(monkeypatch, missing_path):
    class CountOnlyPages:
        def __len__(self):
            return 3

        def __iter__(self):
            pytest.fail("Over-limit page count must be refused before walking pages")

    document = ResourceDocument(CountOnlyPages())
    use_document(monkeypatch, document)
    outcome = PdfResourceProbe(PdfLimits(max_pages=2)).check(missing_path)
    assert outcome.codes == (Code.TOO_MANY_PAGES,)
    assert dict(outcome.facts) == {"page_count": 3}
    assert document.closed


@pytest.mark.parametrize("password", [None, "", "wrong", "secret"])
def test_real_password_handling(pdf_factory, password):
    path = pdf_factory(encryption=pikepdf.Encryption(user="secret", owner="owner", R=6))
    before = path.read_bytes()
    outcome = PdfResourceProbe().check(path, password=password)
    if password == "secret":
        assert outcome.codes == ()
        assert dict(outcome.facts) == {"page_count": 1}
    else:
        assert outcome.codes == (Code.UNREADABLE,)
        assert dict(outcome.facts) == {}
        assert dict(outcome.findings[0].detail) == {"path": path.name}
    assert path.read_bytes() == before


@pytest.mark.parametrize("password", [None, "", "secret"])
def test_strict_open_and_password_forwarding(monkeypatch, missing_path, password):
    document = ResourceDocument([])
    calls = []

    def open_document(*args, **kwargs):
        calls.append((args, kwargs))
        return document

    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=open_document))
    assert PdfResourceProbe().check(missing_path, password=password).codes == ()
    assert calls == [((missing_path,), {"password": password or "", "attempt_recovery": False})]
    assert document.closed


@pytest.mark.parametrize("format_name,normalized", [("docx", "docx"), (" .JPG ", "jpeg"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=forbidden_operation))
    outcome = PdfResourceProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.pdf", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        PdfResourceProbe().check(bad_path)


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_and_corrupt_pdf_are_unreadable(file_factory, missing_path, kind):
    path = missing_path if kind == "missing" else file_factory("broken.pdf", b"not a PDF")
    outcome = PdfResourceProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


def test_repeated_calls_and_policy_are_instance_local(pdf_factory):
    policy = PdfLimits(max_pages=1)
    probe = PdfResourceProbe(policy)
    too_many = pdf_factory("large.pdf", pages=2)
    small = pdf_factory("small.pdf")
    assert probe.check(too_many).codes == (Code.TOO_MANY_PAGES,)
    assert probe.check(small).codes == ()
    assert probe.check(too_many).facts["page_count"] == 2
    assert PdfResourceProbe().check(too_many).codes == ()
    assert probe.policy is policy
    assert probe.name == "validation_resource_limits_pdf"
    assert probe.handles == frozenset({"pdf"})
