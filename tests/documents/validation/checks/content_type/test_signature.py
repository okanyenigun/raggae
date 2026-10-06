"""Detection semantics without handing untrusted bytes to format parsers."""

import errno
import os
from pathlib import Path
import zipfile

from PIL import Image
import pytest

from raggae.documents.validation import ContentTypePolicy, SignatureContentTypeDetector
from raggae.documents.validation.checks.content_type import signature as signature_module
from raggae.documents.validation.checks.content_type.policy import ContentTypeFinding as Code


SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "png", True), (b"%PDF-", "pdf", True),
    (b"\xff\xd8\xff", "jpeg", True), (b"II*\x00", "tiff", True), (b"MM\x00*", "tiff", True),
    (b"PK\x03\x04", "zip", False), (b"PK\x05\x06", "zip", False), (b"PK\x07\x08", "zip", False),
    (b"MZ", "exe", False), (b"\x7fELF", "elf", False),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole", False),
    (b"\x1f\x8b", "gzip", False), (b"Rar!\x1a\x07", "rar", False), (b"7z\xbc\xaf\x27\x1c", "7z", False),
]


@pytest.mark.parametrize("magic,format_name,allowed", SIGNATURES)
@pytest.mark.parametrize("claim", [None, "pdf"])
def test_every_signature_is_identified_before_text_or_extension(file_factory, magic, format_name, allowed, claim):
    data = magic + b"\x00not a structurally valid document"
    path = file_factory(content=data)
    outcome = SignatureContentTypeDetector().check(path, claim)
    expected = (() if allowed else (Code.NOT_ALLOWED,)) + (
        (Code.EXTENSION_MISMATCH,) if claim is not None and claim != format_name else ()
    )
    assert outcome.codes == expected
    assert dict(outcome.facts) == {"detected_format": format_name}
    if not allowed:
        assert outcome.findings[0].detail["format"] == format_name
    if Code.EXTENSION_MISMATCH in expected:
        assert dict(outcome.findings[-1].detail) == {"claimed": claim, "detected": format_name}
    assert path.read_bytes() == data


@pytest.mark.parametrize("magic,claim,detected", [
    (b"%PDF-", " .PDF ", "pdf"), (b"%PDF-", "pdf", "pdf"),
    (b"\xff\xd8\xff", " .JpG ", "jpeg"), (b"\xff\xd8\xff", "JPEG", "jpeg"),
    (b"II*\x00", " .TiF ", "tiff"), (b"MM\x00*", "TIFF", "tiff"),
])
def test_matching_case_dots_spaces_and_aliases_do_not_mismatch(file_factory, magic, claim, detected):
    outcome = SignatureContentTypeDetector().check(file_factory(content=magic), claim)
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == detected


@pytest.mark.parametrize("claim", ["txt", "png", "", " "])
def test_supplied_mismatching_claim_is_preserved_in_detail(file_factory, claim):
    outcome = SignatureContentTypeDetector().check(file_factory(content=b"%PDF-1.7"), claim)
    assert outcome.codes == (Code.EXTENSION_MISMATCH,)
    assert dict(outcome.findings[0].detail) == {"claimed": claim, "detected": "pdf"}


@pytest.mark.parametrize("part,detected", [("word/document.xml", "docx"), ("xl/workbook.xml", "xlsx"), ("ppt/presentation.xml", "pptx")])
def test_real_ooxml_containers_infer_subtype_from_part_names(tmp_path, part, detected):
    path = tmp_path / f"document.{detected}"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(part, "<document/>")
    before = path.read_bytes()
    outcome = SignatureContentTypeDetector().check(path, detected)
    assert outcome.codes == (Code.SUBTYPE_ASSUMED,)
    assert dict(outcome.findings[0].detail) == {"format": detected}
    assert dict(outcome.facts) == {"detected_format": detected}
    assert path.read_bytes() == before


@pytest.mark.parametrize("prefix_size,detected,codes", [
    (16, "zip", (Code.NOT_ALLOWED,)), (32, "docx", (Code.SUBTYPE_ASSUMED,)),
])
def test_ooxml_marker_outside_read_window_cannot_determine_subtype(file_factory, prefix_size, detected, codes):
    path = file_factory(content=b"PK\x03\x04" + b"x" * 12 + b"word/document.xml")
    outcome = SignatureContentTypeDetector(ContentTypePolicy(prefix_bytes=prefix_size)).check(path)
    assert outcome.codes == codes
    assert outcome.facts["detected_format"] == detected


def test_multiple_markers_follow_declared_marker_priority(file_factory):
    path = file_factory(content=b"PK\x03\x04ppt/presentation.xml xl/workbook.xml word/document.xml")
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == (Code.SUBTYPE_ASSUMED,)
    assert outcome.facts["detected_format"] == "docx"


def test_subtype_allowlist_and_claim_findings_all_survive(file_factory):
    path = file_factory(content=b"PK\x03\x04word/document.xml")
    outcome = SignatureContentTypeDetector(ContentTypePolicy(allowed_formats={"pdf"})).check(path, "pdf")
    assert outcome.codes == (Code.SUBTYPE_ASSUMED, Code.NOT_ALLOWED, Code.EXTENSION_MISMATCH)
    assert outcome.facts["detected_format"] == "docx"


@pytest.mark.parametrize("claim", ["txt", "md", "csv", "html", "json", "jsonl"])
@pytest.mark.parametrize("trust", [False, True])
def test_plain_text_extension_trust_for_all_text_formats(file_factory, claim, trust):
    path = file_factory(content=b"plain text with no distinguishing signature")
    outcome = SignatureContentTypeDetector(ContentTypePolicy(trust_extension_for_text=trust)).check(path, f" .{claim.upper()} ")
    detected = claim if trust else "txt"
    assert outcome.facts["detected_format"] == detected
    assert outcome.codes == (() if detected == claim else (Code.EXTENSION_MISMATCH,))


@pytest.mark.parametrize("content,expected", [
    (b"plain text", "txt"), ("Résumé 😀".encode(), "txt"),
    (b" \t\r\n<HTML><body>hello</body></HTML>", "html"),
    (b" \n{\"item\": 1}", "json"), (b" \t[1, 2]", "json"),
])
@pytest.mark.parametrize("trust", [False, True])
def test_text_heuristics_without_a_claim(file_factory, content, expected, trust):
    outcome = SignatureContentTypeDetector(ContentTypePolicy(trust_extension_for_text=trust)).check(file_factory(content=content))
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"detected_format": expected}


@pytest.mark.parametrize("content,inferred", [(b"<html/>", "html"), (b"[1]", "json")])
def test_text_extension_trust_can_override_heuristic_but_not_signature(file_factory, content, inferred):
    path = file_factory(content=content)
    trusted = SignatureContentTypeDetector().check(path, "csv")
    untrusted = SignatureContentTypeDetector(ContentTypePolicy(trust_extension_for_text=False)).check(path, "csv")
    assert trusted.codes == ()
    assert trusted.facts["detected_format"] == "csv"
    assert untrusted.codes == (Code.EXTENSION_MISMATCH,)
    assert untrusted.facts["detected_format"] == inferred


@pytest.mark.parametrize("content", [b"", b"\x00unknown bytes", b"left\xff" + b"x" * 20])
@pytest.mark.parametrize("require_known", [False, True])
def test_unknown_or_empty_input_has_no_invented_format_fact(file_factory, content, require_known):
    outcome = SignatureContentTypeDetector(ContentTypePolicy(require_known_format=require_known)).check(file_factory(content=content), "pdf")
    assert outcome.codes == ((Code.UNDETERMINED,) if require_known else ())
    assert dict(outcome.facts) == {}


def test_empty_allowlist_reports_even_recognized_allowed_by_default_formats(file_factory):
    outcome = SignatureContentTypeDetector(ContentTypePolicy(allowed_formats=set())).check(file_factory(content=b"%PDF-"))
    assert outcome.codes == (Code.NOT_ALLOWED,)
    assert outcome.facts["detected_format"] == "pdf"


@pytest.mark.parametrize("byte", [9, 10, 11, 12, 13])
def test_permitted_text_whitespace_control_bytes(file_factory, byte):
    outcome = SignatureContentTypeDetector().check(file_factory(content=b"left" + bytes([byte]) + b"right"))
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == "txt"


@pytest.mark.parametrize("byte", sorted(set(range(32)) - {9, 10, 11, 12, 13}))
def test_binary_control_bytes_prevent_text_fallback(file_factory, byte):
    outcome = SignatureContentTypeDetector().check(file_factory(content=b"left" + bytes([byte]) + b"right"))
    assert outcome.codes == (Code.UNDETERMINED,)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("content", [
    b"\xef\xbb\xbfhello", "hello".encode("utf-16"), b"\xfe\xff" + "hello".encode("utf-16-be"),
])
@pytest.mark.parametrize("claim", [None, "csv"])
def test_known_text_boms_are_recognized_before_binary_controls(file_factory, content, claim):
    outcome = SignatureContentTypeDetector().check(file_factory(content=content), claim)
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == (claim or "txt")


@pytest.mark.parametrize("character,partial_length", [("é", 1), ("€", 1), ("€", 2), ("😀", 1), ("😀", 2), ("😀", 3)])
def test_valid_utf8_character_cut_by_read_window_is_still_text(file_factory, character, partial_length):
    data = b"a" * (16 - partial_length) + character.encode("utf-8") + b"rest"
    outcome = SignatureContentTypeDetector(ContentTypePolicy(prefix_bytes=16)).check(file_factory(content=data))
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"detected_format": "txt"}


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "TIFF"])
def test_real_small_images_have_the_expected_signature(tmp_path, format_name):
    path = tmp_path / f"small.{format_name.lower()}"
    with Image.new("RGB", (2, 2), color="white") as image:
        image.save(path, format=format_name)
    before = path.read_bytes()
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == format_name.lower()
    assert path.read_bytes() == before


def test_real_small_pdf_has_pdf_signature(tmp_path):
    from pypdf import PdfWriter

    path = tmp_path / "small.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    before = path.read_bytes()
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == "pdf"
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad_path", ["missing", "directory"])
def test_missing_or_directory_path_is_unreadable(tmp_path, bad_path):
    path = tmp_path / bad_path if bad_path == "missing" else tmp_path
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert outcome.findings[0].detail["path"] == str(path)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO, errno.ENOENT])
def test_open_errors_are_unreadable(file_factory, monkeypatch, error_number):
    path = file_factory()

    def failing_open(*args, **kwargs):
        raise OSError(error_number, "controlled open failure")

    monkeypatch.setattr(signature_module.os, "open", failing_open)
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert outcome.findings[0].detail["path"] == str(path)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("value", [None, b"document.txt", 1, object()])
def test_invalid_path_types_are_rejected(value):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        SignatureContentTypeDetector().check(value)


class ReadSpy:
    def __init__(self, raw, error):
        self.raw, self.error, self.read_sizes, self.read_lengths = raw, error, [], []

    def read(self, size):
        self.read_sizes.append(size)
        if self.error is not None:
            raise self.error
        data = self.raw.read(size)
        self.read_lengths.append(len(data))
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.raw.close()


def observe_prefix_reads(monkeypatch, error=None):
    original, opened = os.fdopen, []

    def recording_fdopen(fd, *args, **kwargs):
        spy = ReadSpy(original(fd, *args, **kwargs), error)
        opened.append(spy)
        return spy

    monkeypatch.setattr(signature_module.os, "fdopen", recording_fdopen)
    return opened


@pytest.mark.parametrize("as_string", [False, True])
def test_only_configured_prefix_is_read_once_and_closed(file_factory, monkeypatch, as_string):
    data = b"a" * 16 + b"\x00binary beyond the prefix"
    path = file_factory(content=data)
    opened = observe_prefix_reads(monkeypatch)
    outcome = SignatureContentTypeDetector(ContentTypePolicy(prefix_bytes=16)).check(str(path) if as_string else path)
    assert outcome.codes == ()
    assert outcome.facts["detected_format"] == "txt"
    assert len(opened) == 1
    assert opened[0].read_sizes == [16]
    assert opened[0].read_lengths == [16]
    assert opened[0].raw.closed
    assert path.read_bytes() == data


def test_read_error_closes_descriptor_and_becomes_unreadable(file_factory, monkeypatch):
    path = file_factory()
    opened = observe_prefix_reads(monkeypatch, OSError(errno.EIO, "controlled read failure"))
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert len(opened) == 1
    assert opened[0].raw.closed


def test_prefix_open_uses_readonly_and_nonblocking_flags(file_factory, monkeypatch):
    path = file_factory()
    original_open, flags_seen = os.open, []

    def recording_open(target, flags, *args, **kwargs):
        if Path(target) == path:
            flags_seen.append(flags)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(signature_module.os, "open", recording_open)
    assert SignatureContentTypeDetector().check(path).codes == ()
    assert len(flags_seen) == 1
    assert flags_seen[0] & os.O_ACCMODE == os.O_RDONLY
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    assert flags_seen[0] & nonblocking == nonblocking


@pytest.mark.parametrize("data,detected", [(b"%PDF-invalid body", "pdf"), (b"PK\x03\x04word/document.xml", "docx"), (b"\x89PNG\r\n\x1a\ninvalid body", "png")])
def test_detection_does_not_invoke_format_parsers(file_factory, monkeypatch, forbidden_operation, data, detected):
    import pikepdf
    import pypdf

    path = file_factory(content=data)
    monkeypatch.setattr(zipfile, "ZipFile", forbidden_operation)
    monkeypatch.setattr(Image, "open", forbidden_operation)
    monkeypatch.setattr(pypdf, "PdfReader", forbidden_operation)
    monkeypatch.setattr(pikepdf, "open", forbidden_operation)
    monkeypatch.setattr(pikepdf, "Pdf", SimplePdfGuard(forbidden_operation))
    outcome = SignatureContentTypeDetector().check(path)
    assert outcome.facts["detected_format"] == detected
    assert not outcome.found(Code.UNREADABLE)


class SimplePdfGuard:
    def __init__(self, forbidden_operation):
        self.open = forbidden_operation


def test_all_setters_override_instance_rules_without_changing_another_worker(file_factory):
    default, changed = SignatureContentTypeDetector(), SignatureContentTypeDetector()
    policy = ContentTypePolicy()
    assert SignatureContentTypeDetector(policy).policy is policy
    assert changed.name == "validator_content_type_signature"

    changed.set_signatures(((b"CUSTOM", "pdf"), (b"CUSTOM-MORE", "png")))
    custom = file_factory(content=b"CUSTOM-MORE\x00")
    assert changed.check(custom).facts["detected_format"] == "pdf"
    assert default.check(custom).codes == (Code.UNDETERMINED,)

    changed.set_signatures(((b"PK\x03\x04", "zip"),))
    changed.set_ooxml_markers(((b"ppt/", "pptx"), (b"word/", "docx")))
    archive = file_factory("container.docx", b"PK\x03\x04word/document.xml ppt/presentation.xml")
    assert changed.check(archive).facts["detected_format"] == "pptx"
    assert default.check(archive).facts["detected_format"] == "docx"

    changed.set_text_boms(())
    bom = file_factory("bom.txt", b"\xff\xfeh\x00i\x00")
    assert changed.check(bom).codes == (Code.UNDETERMINED,)
    assert default.check(bom).facts["detected_format"] == "txt"
    changed.set_text_boms((b"\x00CUSTOM-BOM",))
    assert changed.check(file_factory("custom-bom.txt", b"\x00CUSTOM-BOMhello")).facts["detected_format"] == "txt"

    changed.set_binary_control_bytes(frozenset({ord("#")}))
    marker = file_factory("marker.txt", b"hello#world")
    assert changed.check(marker).codes == (Code.UNDETERMINED,)
    assert default.check(marker).facts["detected_format"] == "txt"
    changed.set_binary_control_bytes(frozenset())
    assert changed.check(file_factory("null.txt", b"hello\x00world")).facts["detected_format"] == "txt"


def test_repeated_calls_do_not_share_detection_results(file_factory):
    worker = SignatureContentTypeDetector()
    binary = worker.check(file_factory("binary.bin", b"\x00unknown"))
    pdf = worker.check(file_factory("document.pdf", b"%PDF-1.7"), "pdf")
    text = worker.check(file_factory("text.txt", b"hello"), "txt")
    assert binary.codes == (Code.UNDETERMINED,)
    assert dict(binary.facts) == {}
    assert pdf.codes == text.codes == ()
    assert pdf.facts["detected_format"] == "pdf"
    assert text.facts["detected_format"] == "txt"
