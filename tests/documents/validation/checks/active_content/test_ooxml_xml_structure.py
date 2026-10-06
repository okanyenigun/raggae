from pathlib import Path
import urllib.request

import pytest

from raggae.documents.validation import ActiveContentPolicy, OoxmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code
from .test_ooxml import REL_NS, TYPE_PREFIX, REMOTE, relationships


ROW = f'<Relationship Id="r1" Type="{TYPE_PREFIX}hyperlink" Target="{REMOTE}" TargetMode="External"/>'


@pytest.mark.parametrize("content", [
    f'<Relationships>{ROW}</Relationships>',
    f'<Relationships xmlns="{REL_NS}">{ROW}</Relationships>',
    f'<r:Relationships xmlns:r="{REL_NS}">{ROW.replace("<Relationship", "<r:Relationship")}</r:Relationships>',
])
def test_unqualified_default_and_prefixed_namespaces(ooxml_factory, content):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": REMOTE}


@pytest.mark.parametrize("content", [
    f'<Relationships><!--{ROW}--></Relationships>',
    f'<Relationships><![CDATA[{ROW}]]></Relationships>',
    f'<Other>{ROW}</Other>',
    f'<Relationships><Wrapper>{ROW}</Wrapper></Relationships>',
    f'<Relationships xmlns="{REL_NS}" xmlns:f="urn:foreign">{ROW.replace("<Relationship", "<f:Relationship")}</Relationships>',
])
def test_non_relationship_text_or_nodes_do_not_invent_references(ooxml_factory, content):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    assert OoxmlActiveContentProbe().check(path).codes == ()


@pytest.mark.parametrize("content", ["", "<Relationships>", "<Relationships></Other>",
                                        f'<Relationships>{ROW.replace("Id=", "Id=\"duplicate\" Id=")}</Relationships>'])
def test_malformed_xml_returns_unreadable_without_partial_findings(ooxml_factory, content):
    path = ooxml_factory(entries=[("word/vbaProject.bin", b"opaque macro"),
                                 ("word/_rels/document.xml.rels", content)])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


@pytest.mark.parametrize("doctype", [
    '<!DOCTYPE Relationships>',
    '<!DOCTYPE Relationships [<!ENTITY target "https://evil.test/file">]>',
    '<!DOCTYPE Relationships SYSTEM "https://evil.test/relationships.dtd">',
    '<!DOCTYPE Relationships [<!ENTITY target SYSTEM "file:///not-readable">]>',
])
def test_dtds_are_refused_without_fetching_or_reading_entities(ooxml_factory, monkeypatch, forbidden_operation, doctype):
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", doctype + f'<Relationships>{ROW}</Relationships>')])
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    monkeypatch.setattr(Path, "read_bytes", forbidden_operation)
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert "DTD declarations are not allowed" in outcome.findings[0].message


def test_decoded_host_is_used_for_allowlisting(ooxml_factory):
    content = relationships(("hyperlink", "https://&#x65;vil.test/file", "External"))
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    default = OoxmlActiveContentProbe().check(path)
    assert default.findings[0].detail["sample"] == "https://evil.test/file"
    policy = ActiveContentPolicy(allowed_reference_hosts={"evil.test"})
    assert OoxmlActiveContentProbe(policy).check(path).codes == ()


def test_references_across_parts_are_deduplicated_after_entity_decoding(ooxml_factory):
    path = ooxml_factory(entries=[
        ("word/_rels/document.xml.rels", relationships(("hyperlink", "https://evil.test/&#x66;ile", "External"))),
        ("word/_rels/settings.xml.rels", relationships(("hyperlink", "https://evil.test/file", "External"))),
    ])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": "https://evil.test/file"}


def test_invalid_xml_does_not_leak_state_into_later_calls(ooxml_factory):
    broken = ooxml_factory("broken.docx", entries=[("word/_rels/document.xml.rels", "<Relationships>")])
    clean = ooxml_factory("clean.docx", entries=[("word/_rels/document.xml.rels", relationships())])
    probe = OoxmlActiveContentProbe()
    assert probe.check(broken).codes == (Code.UNREADABLE,)
    assert probe.check(clean).codes == ()
