import struct
import urllib.request
import zipfile

import pytest

from raggae.documents.validation import ActiveContentPolicy, OoxmlActiveContentProbe
from raggae.documents.validation.checks.active_content import ooxml as ooxml_module
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPE_PREFIX = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REMOTE = "https://evil.test/document"


def relationships(*items):
    rows = [f'<Relationship Id="r{index}" Type="{TYPE_PREFIX}{kind}" Target="{target}" TargetMode="{mode}"/>'
            for index, (kind, target, mode) in enumerate(items)]
    return f'<Relationships xmlns="{REL_NS}">' + "".join(rows) + '</Relationships>'


@pytest.mark.parametrize("format_name", [None, "docx", "xlsx", "pptx", " .DOCX "])
@pytest.mark.parametrize("as_string", [False, True])
def test_clean_part_archive_supported_formats_and_source_preservation(ooxml_factory, format_name, as_string):
    path = ooxml_factory(entries=[("word/document.xml", "<document/>"), ("_rels/.rels", relationships())])
    before = path.read_bytes()
    outcome = OoxmlActiveContentProbe().check(str(path) if as_string else path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("names", [["word/vbaProject.bin"], ["xl/vbaProject.bin"],
                                  ["ppt/vbaProject.bin"], ["word/vbaProject.bin", "xl/vbaProject.bin"]])
def test_macros_are_reported_once_without_reading_payloads(ooxml_factory, monkeypatch, forbidden_operation, names):
    path = ooxml_factory(entries=[(name, b"never execute or read this") for name in names])
    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_operation)
    assert OoxmlActiveContentProbe().check(path).codes == (Code.MACRO,)


@pytest.mark.parametrize("kind", ["hyperlink", "image"])
@pytest.mark.parametrize("mode", ["Internal", "External"])
def test_internal_relationships_are_not_outbound_references(ooxml_factory, kind, mode):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", relationships((kind, REMOTE, mode)))])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == ((Code.REMOTE_REFERENCE,) if mode == "External" else ())
    if mode == "External":
        assert dict(outcome.findings[0].detail) == {"count": 1, "sample": REMOTE}


@pytest.mark.parametrize("allow_host", [False, True])
def test_template_classification_is_separate_from_allowlisting(ooxml_factory, allow_host):
    path = ooxml_factory(entries=[("word/_rels/settings.xml.rels", relationships(("attachedTemplate", REMOTE, "External")))])
    policy = ActiveContentPolicy(allowed_reference_hosts={"evil.test"} if allow_host else set())
    outcome = OoxmlActiveContentProbe(policy).check(path)
    assert outcome.codes == ((Code.REMOTE_TEMPLATE,) if allow_host else (Code.REMOTE_TEMPLATE, Code.REMOTE_REFERENCE))
    assert dict(outcome.findings[0].detail) == {"target": REMOTE}


def test_macros_templates_and_unique_references_are_combined(ooxml_factory):
    first = "https://z.evil.test/template"
    second = "https://a.evil.test/template"
    path = ooxml_factory(entries=[
        ("word/vbaProject.bin", b"opaque macro"),
        ("word/_rels/settings.xml.rels", relationships(("attachedTemplate", first, "External"))),
        ("word/_rels/document.xml.rels", relationships(("attachedTemplate", second, "External"),
                                                       ("hyperlink", first, "External"))),
    ])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.MACRO, Code.REMOTE_TEMPLATE, Code.REMOTE_REFERENCE)
    assert dict(outcome.findings[1].detail) == {"target": first}
    assert dict(outcome.findings[2].detail) == {"count": 2, "sample": f"{second}, {first}"}


def test_only_relationship_parts_are_read_and_nothing_is_fetched(ooxml_factory, monkeypatch, forbidden_operation):
    rel_path = "word/_rels/document.xml.rels"
    path = ooxml_factory(entries=[("word/document.xml", b"not read"), ("word/vbaProject.bin", b"not read"),
                                 ("word/media/image.png", b"not decoded"),
                                 (rel_path, relationships(("hyperlink", REMOTE, "External")))])
    original_read = zipfile.ZipFile.read
    reads = []

    def read(archive, name, *args, **kwargs):
        assert name.endswith(".rels")
        reads.append(name)
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden_operation)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden_operation)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    assert OoxmlActiveContentProbe().check(path).codes == (Code.MACRO, Code.REMOTE_REFERENCE)
    assert reads == [rel_path]


@pytest.mark.parametrize("content", [relationships(("hyperlink", "", "External")), "<Relationships/>", "<other/>"])
def test_empty_targets_and_unrelated_xml_do_not_invent_references(ooxml_factory, content):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    assert OoxmlActiveContentProbe().check(path).codes == ()


def test_bad_utf8_does_not_prevent_scanning_other_relationship_text(ooxml_factory):
    content = relationships(("hyperlink", REMOTE, "External")).replace("</Relationships>", "<!--invalid encoding--></Relationships>")
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content.encode("utf-8").replace(b"invalid encoding", b"\xff"))])
    assert OoxmlActiveContentProbe().check(path).codes == (Code.REMOTE_REFERENCE,)


@pytest.mark.parametrize("password", [None, "", "irrelevant"])
def test_password_is_irrelevant_for_unencrypted_zip(ooxml_factory, password):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", relationships(("hyperlink", REMOTE, "External")))])
    assert OoxmlActiveContentProbe().check(path, password=password) == OoxmlActiveContentProbe().check(path)


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_and_corrupt_container_are_unreadable(file_factory, missing_path, kind):
    path = missing_path if kind == "missing" else file_factory("broken.docx", b"not a ZIP")
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


def test_bad_relationship_crc_is_unreadable(ooxml_factory):
    member = "word/_rels/document.xml.rels"
    path = ooxml_factory(entries=[(member, relationships(("hyperlink", REMOTE, "External")))])
    content = bytearray(path.read_bytes())
    central = content.index(b"PK\x01\x02")
    crc = struct.unpack_from("<I", content, central + 16)[0]
    struct.pack_into("<I", content, central + 16, crc ^ 1)
    path.write_bytes(content)
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(zipfile.BadZipFile, match="CRC"):
            archive.read(member)
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("stage", ["open", "names", "relationship"])
@pytest.mark.parametrize("error_type", [OSError, zipfile.BadZipFile, RuntimeError])
def test_archive_or_encrypted_member_errors_are_unreadable_and_close(monkeypatch, missing_path, stage, error_type):
    class Archive:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        def namelist(self):
            if stage == "names":
                raise error_type("cannot inspect archive")
            return ["word/_rels/document.xml.rels"]

        def read(self, name):
            raise error_type("cannot inspect relationship member")

    archive = Archive()

    def open_archive(*args, **kwargs):
        if stage == "open":
            raise error_type("cannot open archive")
        return archive

    monkeypatch.setattr(ooxml_module.zipfile, "ZipFile", open_archive)
    outcome = OoxmlActiveContentProbe().check(missing_path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}
    assert archive.closed == (stage != "open")


@pytest.mark.parametrize("format_name,normalized", [("zip", "zip"), (" .PDF ", "pdf"), ("jpg", "jpeg"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(ooxml_module.zipfile, "ZipFile", forbidden_operation)
    outcome = OoxmlActiveContentProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.docx", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        OoxmlActiveContentProbe().check(bad_path)


def test_repeated_calls_and_policy_are_instance_local(ooxml_factory):
    policy = ActiveContentPolicy(allowed_reference_hosts={"evil.test"})
    probe = OoxmlActiveContentProbe(policy)
    remote = ooxml_factory("remote.docx", entries=[("word/_rels/document.xml.rels", relationships(("hyperlink", REMOTE, "External")))])
    clean = ooxml_factory("clean.docx")
    assert probe.check(remote).codes == ()
    assert probe.check(clean).codes == ()
    assert OoxmlActiveContentProbe().check(remote).codes == (Code.REMOTE_REFERENCE,)
    assert probe.policy is policy
    assert probe.name == "validation_active_content_ooxml"
    assert probe.handles == frozenset({"docx", "xlsx", "pptx"})
