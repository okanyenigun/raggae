import codecs

import pytest

from raggae.documents.validation import HtmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("content,codes", [
    ('<html><body><script>test()</script></body></html>', (Code.SCRIPT,)),
    ('<html><body><script src="https://evil.test/file"></script></body></html>',
     (Code.SCRIPT, Code.REMOTE_REFERENCE)),
])
@pytest.mark.parametrize("encoding,bom", [("utf-8", codecs.BOM_UTF8),
                                         ("utf-16-le", codecs.BOM_UTF16_LE),
                                         ("utf-16-be", codecs.BOM_UTF16_BE)])
def test_bom_marked_html_does_not_hide_active_content(file_factory, content, codes, encoding, bom):
    data = bom + content.encode(encoding)
    assert data.startswith(bom)
    assert data[len(bom):].decode(encoding, errors="strict") == content
    path = file_factory("encoded.html", data)
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == codes
    if Code.REMOTE_REFERENCE in codes:
        assert dict(outcome.findings[-1].detail) == {"count": 1, "sample": "https://evil.test/file"}
    assert path.read_bytes() == data
