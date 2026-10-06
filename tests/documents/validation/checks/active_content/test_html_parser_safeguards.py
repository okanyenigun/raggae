import pytest

from raggae.documents.validation import ActiveContentPolicy, HtmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("content,codes", [
    ('<!-- <iframe src="https://ignored.test/"></iframe> --><script>test()</script>', (Code.SCRIPT,)),
    ('<!-- <script>comment()</script> --><script>real()</script>', (Code.SCRIPT,)),
    ('<script>const example = "<iframe src=\'https://ignored.test/\'></iframe>";</script>', (Code.SCRIPT,)),
    ('<script>const example = "onclick=\'test()\'";</script>', (Code.SCRIPT,)),
    ('<style>p::before { content: "<script>test()</script>"; }</style>', ()),
    ('<p title="<script>test()</script>">example</p>', ()),
    ('<button title="onload=example" onclick="test()">button</button>', (Code.EVENT_HANDLER,)),
    ('<SCRIPT src = "//evil.test/file"/>', (Code.SCRIPT, Code.REMOTE_REFERENCE)),
    ('<EMBED/>', (Code.EMBEDDED_FRAME,)),
    ('<!DOCTYPE html><p>normal document</p>', ()),
])
def test_actual_features_survive_while_raw_text_is_ignored(file_factory, content, codes):
    path = file_factory(content=content.encode("utf-8"))
    assert HtmlActiveContentProbe().check(path, "html").codes == codes


@pytest.mark.parametrize("content,expected", [
    ('<a href="&#x68;ttps://evil.test/file?a=1&amp;b=2">link</a>', "https://evil.test/file?a=1&b=2"),
    ('<a href="  https://evil.test/file  ">link</a>', "https://evil.test/file"),
    ('<img SRC=//evil.test/file>', "https://evil.test/file"),
])
def test_reference_attribute_decoding_and_whitespace(file_factory, content, expected):
    path = file_factory(content=content.encode("utf-8"))
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": expected}


def test_allowlist_uses_decoded_host_without_hiding_real_script(file_factory):
    path = file_factory(content=b'<script src="https://&#x65;vil.test/file"></script>')
    policy = ActiveContentPolicy(allowed_reference_hosts={"evil.test"})
    assert HtmlActiveContentProbe(policy).check(path, "html").codes == (Code.SCRIPT,)


def test_comment_only_result_does_not_leak_state_into_repeated_calls(file_factory):
    comments = file_factory("comments.html", b'<!-- <script src="https://ignored.test/"></script> -->')
    active = file_factory("active.html", b'<script src="https://evil.test/"></script>')
    probe = HtmlActiveContentProbe()
    assert probe.check(comments, "html").codes == ()
    assert probe.check(active, "html").codes == (Code.SCRIPT, Code.REMOTE_REFERENCE)
    assert probe.check(comments, "html").codes == ()
