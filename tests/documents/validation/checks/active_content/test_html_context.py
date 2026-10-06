from html.parser import HTMLParser

import pytest

from raggae.documents.validation import HtmlActiveContentProbe


class HtmlStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attributes):
        self.tags.append(tag)
        self.attributes.extend(attributes)


@pytest.mark.parametrize("content", [
    '<html><!-- <script>test()</script> --><body>Hello</body></html>',
    '<p>Hello</p><!-- <iframe src="https://evil.test/frame"></iframe> -->',
    '<!-- <button onclick="test()">example</button> --><p>Hello</p>',
    '<p>Example attribute: onclick="test()"</p>',
    '<img data-src="https://evil.test/image.png">',
    '<p data-onclick="test()">Hello</p>',
    '<p>&lt;script&gt;test()&lt;/script&gt;</p>',
], ids=["comment-script", "comment-frame", "comment-handler", "displayed-handler",
        "data-src", "data-onclick", "escaped-script"])
def test_non_active_html_context_is_not_reported_as_active_content(file_factory, content):
    structure = HtmlStructure()
    structure.feed(content)
    structure.close()
    assert not {"script", "iframe", "object", "embed"}.intersection(structure.tags)
    assert not any(name.startswith("on") for name, _ in structure.attributes)
    assert not any(name in {"src", "href", "action"} for name, _ in structure.attributes)
    path = file_factory("example.html", content.encode("utf-8"))
    before = path.read_bytes()
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == ()
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before
