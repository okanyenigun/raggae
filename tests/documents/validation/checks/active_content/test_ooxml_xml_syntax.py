from itertools import permutations
import xml.etree.ElementTree as ET

import pytest

from raggae.documents.validation import OoxmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code
from .test_ooxml import REL_NS, TYPE_PREFIX, REMOTE


ATTRIBUTE_ORDERS = tuple(permutations(("Type", "Target", "TargetMode")))


@pytest.mark.parametrize("kind", ["hyperlink", "attachedTemplate"])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("order", ATTRIBUTE_ORDERS)
def test_valid_attribute_orders_and_quotes_preserve_relationship_meaning(ooxml_factory, kind, quote, order):
    attributes = {"Type": TYPE_PREFIX + kind, "Target": REMOTE, "TargetMode": "External"}
    fields = " ".join(f"{name}={quote}{attributes[name]}{quote}" for name in order)
    content = f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" {fields}/></Relationships>'
    root = ET.fromstring(content)
    assert root[0].attrib == {"Id": "r1", **attributes}
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    before = path.read_bytes()
    outcome = OoxmlActiveContentProbe().check(path, "docx")
    expected = (Code.REMOTE_REFERENCE,) if kind == "hyperlink" else (Code.REMOTE_TEMPLATE, Code.REMOTE_REFERENCE)
    assert outcome.codes == expected
    assert dict(outcome.findings[-1].detail) == {"count": 1, "sample": REMOTE}
    assert path.read_bytes() == before


@pytest.mark.parametrize("equals", [" = ", "=\n", "\t=\t"])
def test_xml_whitespace_does_not_hide_external_reference(ooxml_factory, equals):
    attributes = {"Type": TYPE_PREFIX + "hyperlink", "Target": REMOTE, "TargetMode": "External"}
    fields = " ".join(f'{name}{equals}"{value}"' for name, value in attributes.items())
    content = f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" {fields}/></Relationships>'
    assert ET.fromstring(content)[0].attrib == {"Id": "r1", **attributes}
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": REMOTE}


@pytest.mark.parametrize("encoded,decoded", [
    ("https://evil.test/file?a=1&amp;b=2", "https://evil.test/file?a=1&b=2"),
    ("https://evil.test/&#x66;ile", "https://evil.test/file"),
    ("https://evil.test/a&apos;b", "https://evil.test/a'b"),
])
def test_reference_target_is_the_decoded_xml_attribute_value(ooxml_factory, encoded, decoded):
    content = (f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" Type="{TYPE_PREFIX}hyperlink" '
               f'Target="{encoded}" TargetMode="External"/></Relationships>')
    assert ET.fromstring(content)[0].attrib["Target"] == decoded
    path = ooxml_factory(entries=[("word/_rels/document.xml.rels", content)])
    outcome = OoxmlActiveContentProbe().check(path)
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": decoded}
