from pathlib import Path
from types import SimpleNamespace

import pikepdf
from pypdf import PdfReader
import pytest

from raggae.documents.validation import CapabilityPolicy, PdfCapabilityProbe, Severity
from raggae.documents.validation.checks.capability import pdf as pdf_module
from raggae.documents.validation.checks.capability.policy import CapabilityFinding as Code


class Page:
    def __init__(self, text=None, resources=None, error=None):
        self.text = text
        self.resources = resources
        self.error = error
        self.extractions = 0
        self.resource_reads = 0

    def extract_text(self):
        self.extractions += 1
        if self.error is not None:
            raise self.error
        return self.text

    def get(self, key):
        assert key == "/Resources"
        self.resource_reads += 1
        return self.resources


def assert_facts(result, *, sampled, qualifying, characters):
    assert dict(result.facts) == {
        "has_extractable_text": qualifying > 0,
        "pages_sampled": sampled,
        "pages_with_text": qualifying,
        "characters_sampled": characters,
    }


@pytest.mark.parametrize("length", [19, 20, 21])
@pytest.mark.parametrize("format_name", [None, "pdf", " .PDF "])
@pytest.mark.parametrize("as_string", [False, True])
def test_real_text_page_character_floor(capability_pdf_factory, length, format_name, as_string):
    path = capability_pdf_factory(texts=("A" * length,))
    # Independent extraction verifies that the generated PDF has a real text layer.
    assert PdfReader(path).pages[0].extract_text().strip() == "A" * length
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(str(path) if as_string else path, format_name)
    qualifies = length >= 20
    assert result.codes == (() if qualifies else (Code.EMPTY_DOCUMENT,))
    assert_facts(result, sampled=1, qualifying=int(qualifies), characters=length)
    if not qualifies:
        assert dict(result.findings[0].detail) == {"pages_sampled": 1}
    assert path.read_bytes() == before


@pytest.mark.parametrize("text,characters", [(None, 0), ("", 0), (" \t\r\n", 0),
                                             (" a\tb\nc\rd ", 4), ("中文 😀", 3)])
def test_whitespace_is_not_counted(monkeypatch, missing_path, text, characters):
    page = Page(text)
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=3)).check(missing_path)
    qualifying = int(characters >= 3)
    assert result.codes == (() if qualifying else (Code.EMPTY_DOCUMENT,))
    assert_facts(result, sampled=1, qualifying=qualifying, characters=characters)
    assert page.extractions == page.resource_reads == 1


@pytest.mark.parametrize("budget,sampled,characters,qualifying", [(1, 1, 4, 0), (2, 2, 9, 1),
                                                                (3, 3, 15, 2), (4, 3, 15, 2)])
def test_sampling_budget_and_per_page_threshold(monkeypatch, missing_path, budget, sampled, characters, qualifying):
    pages = [Page("A" * length) for length in (4, 5, 6)]
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=pages, is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=budget, min_characters_per_page=5)).check(missing_path)
    assert result.codes == (() if qualifying else (Code.EMPTY_DOCUMENT,))
    assert_facts(result, sampled=sampled, qualifying=qualifying, characters=characters)
    assert [page.extractions for page in pages] == [int(index < sampled) for index in range(3)]
    assert [page.resource_reads for page in pages] == [int(index < sampled) for index in range(3)]


def test_short_pages_do_not_qualify_by_combining_lengths(monkeypatch, missing_path):
    pages = [Page("AAAA"), Page("BBBB")]
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=pages, is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(min_characters_per_page=5)).check(missing_path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert_facts(result, sampled=2, qualifying=0, characters=8)


def test_unsampled_page_is_never_inspected(monkeypatch, missing_path, forbidden_operation):
    pages = [Page("AAAAA"), SimpleNamespace(extract_text=forbidden_operation, get=forbidden_operation)]
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=pages, is_encrypted=False))
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=1, min_characters_per_page=5)).check(missing_path)
    assert result.codes == ()
    assert_facts(result, sampled=1, qualifying=1, characters=5)


@pytest.mark.parametrize("budget,qualifying,characters", [(3, 0, 0), (4, 1, 25)])
def test_real_text_layer_only_after_sample_window(capability_pdf_factory, budget, qualifying, characters):
    path = capability_pdf_factory(texts=("", "", "", "A" * 25))
    result = PdfCapabilityProbe(CapabilityPolicy(sampled_pages=budget)).check(path)
    assert result.codes == (() if qualifying else (Code.EMPTY_DOCUMENT,))
    assert_facts(result, sampled=budget, qualifying=qualifying, characters=characters)


@pytest.mark.parametrize("pages", [1, 2, 4])
def test_real_blank_pdf_is_empty(pdf_factory, pages):
    path = pdf_factory(pages=pages)
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert dict(result.findings[0].detail) == {"pages_sampled": min(pages, 3)}
    assert_facts(result, sampled=min(pages, 3), qualifying=0, characters=0)
    assert path.read_bytes() == before


@pytest.mark.parametrize("user,password,opens", [
    (None, None, True), (None, "", True), (None, "irrelevant", True),
    ("user", None, False), ("user", "", False), ("user", "wrong", False),
    ("user", "user", True), ("user", "owner", True),
    ("", None, True), ("", "", True), ("", "owner", True), ("", "wrong", False),
])
def test_real_pdf_encryption_password_combinations(capability_pdf_factory, user, password, opens):
    encryption = None if user is None else pikepdf.Encryption(owner="owner", user=user, R=6)
    path = capability_pdf_factory(encryption=encryption)
    before = path.read_bytes()
    result = PdfCapabilityProbe().check(path, "pdf", password)
    assert result.codes == (() if opens else (Code.UNREADABLE,))
    if opens:
        assert_facts(result, sampled=1, qualifying=1, characters=25)
    else:
        assert dict(result.facts) == {}
        assert dict(result.findings[0].detail) == {"path": path.name}
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_corrupt_pdf_is_unreadable(file_factory, content):
    path = file_factory("corrupt.pdf", content)
    result = PdfCapabilityProbe().check(path, "pdf")
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": path.name}
    assert dict(result.facts) == {}


def test_missing_pdf_is_unreadable(missing_path):
    assert PdfCapabilityProbe().check(missing_path, "pdf").codes == (Code.UNREADABLE,)


@pytest.mark.parametrize("error_type", [OSError, PermissionError, ValueError, RuntimeError])
def test_open_failure_has_no_partial_facts(monkeypatch, missing_path, error_type):
    def fail(path):
        raise error_type("cannot open PDF")

    monkeypatch.setattr(pdf_module, "PdfReader", fail)
    result = PdfCapabilityProbe().check(missing_path, "pdf")
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.facts) == {}


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, OSError])
def test_per_page_extraction_failure_currently_degrades_to_zero(monkeypatch, missing_path, error_type):
    # Characterize the planned degraded-page contract, separately from open errors.
    page = Page(error=error_type("cannot extract text"))
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe().check(missing_path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert_facts(result, sampled=1, qualifying=0, characters=0)


@pytest.mark.parametrize("resources", [None, {}, {"/XObject": {}}, {"/XObject": {"/Other": {"/Subtype": "/Font"}}}])
def test_no_image_metadata_does_not_invent_a_scan(monkeypatch, missing_path, resources):
    page = Page(resources=resources)
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe().check(missing_path)
    assert result.codes == (Code.EMPTY_DOCUMENT,)
    assert_facts(result, sampled=1, qualifying=0, characters=0)


def test_direct_image_dictionary_suppresses_empty_without_decoding(monkeypatch, missing_path, forbidden_operation):
    class Image(dict):
        get_data = forbidden_operation

    page = Page(resources={"/XObject": {"/Im": Image({"/Subtype": "/Image"})}})
    monkeypatch.setattr(pdf_module, "PdfReader", lambda path: SimpleNamespace(pages=[page], is_encrypted=False))
    result = PdfCapabilityProbe().check(missing_path)
    assert result.codes == ()
    assert_facts(result, sampled=1, qualifying=0, characters=0)


@pytest.mark.parametrize("format_name,normalized", [("png", "png"), (" .JPG ", "jpeg"), ("txt", "txt"),
                                                   ("docx", "docx"), ("csv", "csv"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(pdf_module, "PdfReader", forbidden_operation)
    result = PdfCapabilityProbe().check(missing_path, format_name)
    assert result.codes == (Code.NOT_APPLICABLE,)
    assert dict(result.findings[0].detail) == {"detected_format": normalized}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.pdf", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        PdfCapabilityProbe().check(bad_path, "pdf")


def test_repeated_calls_custom_severity_and_properties(capability_pdf_factory):
    policy = CapabilityPolicy(severities={Code.EMPTY_DOCUMENT: Severity.REJECT})
    probe = PdfCapabilityProbe(policy)
    text = capability_pdf_factory("text.pdf")
    blank = capability_pdf_factory("blank.pdf", texts=("",))
    assert probe.check(text).codes == ()
    assert probe.check(blank).codes == (Code.EMPTY_DOCUMENT,)
    assert probe.check(text).codes == ()
    assert probe.policy is policy
    assert probe.name == "validation_capability_pdf"
    assert probe.handles == frozenset({"pdf"})
