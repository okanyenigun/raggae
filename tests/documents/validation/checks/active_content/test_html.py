import errno
from pathlib import Path
import urllib.request
import webbrowser

import pytest

from raggae.documents.validation import ActiveContentPolicy, HtmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("format_name", [None, "html", " .HTML "])
@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize("content", [b"", b"plain text", b"<html><body><p>Hello</p></body></html>"])
def test_clean_html_formats_and_source_preservation(file_factory, format_name, as_string, content):
    path = file_factory("document.html", content)
    before = path.read_bytes()
    outcome = HtmlActiveContentProbe().check(str(path) if as_string else path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("content,codes", [
    ('<script>test()</script>', (Code.SCRIPT,)),
    ('<SCRIPT type="text/javascript">test()</SCRIPT>', (Code.SCRIPT,)),
    ('<iframe></iframe>', (Code.EMBEDDED_FRAME,)),
    ('<object></object>', (Code.EMBEDDED_FRAME,)),
    ('<embed>', (Code.EMBEDDED_FRAME,)),
    ('<body onload="test()"></body>', (Code.EVENT_HANDLER,)),
    ('<button ONCLICK = "test()">button</button>', (Code.EVENT_HANDLER,)),
    ('<script>test()</script><script>other()</script>', (Code.SCRIPT,)),
    ('<iframe></iframe><object></object><embed>', (Code.EMBEDDED_FRAME,)),
])
def test_active_features_are_reported_once(file_factory, content, codes):
    path = file_factory(content=content.encode("utf-8"))
    assert HtmlActiveContentProbe().check(path, "html").codes == codes


@pytest.mark.parametrize("attribute,tag", [("src", "img"), ("href", "a"), ("action", "form")])
@pytest.mark.parametrize("prefix", ["https://", "http://", "//"])
@pytest.mark.parametrize("quote", ['"', "'", ""])
def test_remote_attributes_quoted_unquoted_and_protocol_relative(file_factory, attribute, tag, prefix, quote):
    value = prefix + "evil.test/path"
    content = f'<{tag} {attribute}={quote}{value}{quote}>'
    if tag != "img":
        content += f'</{tag}>'
    path = file_factory(content=content.encode("utf-8"))
    outcome = HtmlActiveContentProbe().check(path, "html")
    sample = "https://evil.test/path" if prefix == "//" else value
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": sample}


@pytest.mark.parametrize("reference", ["ftp://evil.test/file", "mailto:user@evil.test", "file:///tmp/external"])
def test_other_quoted_absolute_references_are_reported(file_factory, reference):
    path = file_factory(content=f'<a href="{reference}">link</a>'.encode("utf-8"))
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 1, "sample": reference}


@pytest.mark.parametrize("reference", ["local.png", "../local.png", "/assets/local.png", "#section"])
def test_relative_and_fragment_references_are_not_remote(file_factory, reference):
    path = file_factory(content=f'<a href="{reference}">link</a>'.encode("utf-8"))
    assert HtmlActiveContentProbe().check(path, "html").codes == ()


@pytest.mark.parametrize("allow", [False, True])
def test_allowlisted_script_source_still_reports_script(file_factory, allow):
    path = file_factory(content=b'<script src="https://cdn.example.com/tracker.js"></script>')
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"} if allow else set())
    outcome = HtmlActiveContentProbe(policy).check(path, "html")
    assert outcome.codes == ((Code.SCRIPT,) if allow else (Code.SCRIPT, Code.REMOTE_REFERENCE))


def test_all_categories_and_deduplicated_sorted_references(file_factory):
    content = ('<script src="https://z.evil.test/file"></script>'
               '<iframe src="https://a.evil.test/frame"></iframe>'
               '<button onclick="test()">button</button>'
               '<a href="https://z.evil.test/file">link</a>')
    path = file_factory(content=content.encode("utf-8"))
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == (Code.SCRIPT, Code.EMBEDDED_FRAME, Code.EVENT_HANDLER, Code.REMOTE_REFERENCE)
    assert dict(outcome.findings[-1].detail) == {"count": 2, "sample": "https://a.evil.test/frame, https://z.evil.test/file"}


def test_reference_sample_is_bounded_but_count_is_complete(file_factory):
    references = [f"https://evil.test/{index}" for index in reversed(range(7))]
    content = "".join(f'<a href="{url}">link</a>' for url in references)
    path = file_factory(content=content.encode("utf-8"))
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == (Code.REMOTE_REFERENCE,)
    assert dict(outcome.findings[0].detail) == {"count": 7, "sample": ", ".join(sorted(references)[:5])}


def test_payloads_are_not_executed_and_references_are_not_fetched(file_factory, monkeypatch, forbidden_operation):
    path = file_factory(content=b'<script src="https://evil.test/tracker.js">test()</script>')
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    monkeypatch.setattr(webbrowser, "open", forbidden_operation)
    outcome = HtmlActiveContentProbe().check(path, "html")
    assert outcome.codes == (Code.SCRIPT, Code.REMOTE_REFERENCE)


@pytest.mark.parametrize("password", [None, "", "irrelevant"])
def test_password_is_irrelevant(file_factory, password):
    path = file_factory(content=b'<script>test()</script>')
    assert HtmlActiveContentProbe().check(path, "html", password) == HtmlActiveContentProbe().check(path, "html")


@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, IsADirectoryError])
def test_read_errors_are_unreadable(monkeypatch, missing_path, error_type):
    def fail(*args, **kwargs):
        raise error_type(errno.EIO, "cannot read HTML")

    monkeypatch.setattr(Path, "read_text", fail)
    outcome = HtmlActiveContentProbe().check(missing_path, "html")
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}


def test_missing_html_is_unreadable(missing_path):
    outcome = HtmlActiveContentProbe().check(missing_path, "html")
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}


def test_utf8_replacement_and_read_parameters(file_factory, monkeypatch):
    path = file_factory(content=b'<p>\xff</p><script>test()</script>')
    original_read = Path.read_text
    calls = []

    def read_text(target, *args, **kwargs):
        calls.append((target, args, kwargs))
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert HtmlActiveContentProbe().check(path, "html").codes == (Code.SCRIPT,)
    assert calls == [(path, (), {"encoding": "utf-8", "errors": "replace"})]


@pytest.mark.parametrize("format_name,normalized", [("pdf", "pdf"), ("csv", "csv"), (" .JPG ", "jpeg"),
                                                   ("htm", "htm"), (" .HTM ", "htm"), ("", "")])
def test_mismatched_format_never_reads(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    outcome = HtmlActiveContentProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.html", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        HtmlActiveContentProbe().check(bad_path, "html")


def test_repeated_calls_and_policy_are_instance_local(file_factory):
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    probe = HtmlActiveContentProbe(policy)
    remote = file_factory("remote.html", b'<a href="https://example.com/file">link</a>')
    clean = file_factory("clean.html", b"<p>hello</p>")
    assert probe.check(remote, "html").codes == ()
    assert probe.check(clean, "html").codes == ()
    assert HtmlActiveContentProbe().check(remote, "html").codes == (Code.REMOTE_REFERENCE,)
    assert probe.policy is policy
    assert probe.name == "validation_active_content_html"
    assert probe.handles == frozenset({"html"})
