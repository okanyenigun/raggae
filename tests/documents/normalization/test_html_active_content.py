"""HTML removals, retained content, exact no-ops, and I/O failure boundaries."""

from io import BytesIO

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    HtmlActiveContentCategory,
    HtmlActiveContentFinding,
    HtmlActiveContentIssue,
    NormalizationWorker,
    HtmlActiveContentReport,
    HtmlActiveContentSanitizer,
    NormalizationRequest,
    NormalizationResult,
    StrictTextEncodingNormalizer,
)
from raggae.documents.normalization.sanitization.html_active_content.rewriter import HtmlRewriter


def normalize(text):
    return HtmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=text.encode("utf-8"), output_path=None,
    ))


@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_all_removals_and_ordinary_content_preserved(source_kind, disk, tmp_path):
    text = (
        '<!DOCTYPE html><html><body onload="run()">'
        '<script src="https://example.invalid/script.js">SECRET_SCRIPT</script>'
        '<iframe>REMOVE_FRAME</iframe><object>REMOVE_OBJECT</object><embed src="media">'
        '<p onclick="run()">Café &amp; &#169; <b>ordinary</b> text.</p>'
        '<a href="https://example.invalid">Keep link text</a><img src="//example.invalid/image">'
        '<a href="/local">Keep relative link</a><!-- Keep comment --></body></html>'
    )
    original = text.encode("utf-8")
    source = original
    if source_kind == "path":
        path = tmp_path / "input.bin"
        path.write_bytes(original)
        source = path
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.html" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = HtmlActiveContentSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.removals == {"script": 1, "embedded_frame": 3, "event_handler": 2, "remote_reference": 2}
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    actual = result.artifact.read_bytes().decode("utf-8")
    assert actual == (
        '<!DOCTYPE html><html><body>'
        '<p>Café &amp; &#169; <b>ordinary</b> text.</p>'
        '<a>Keep link text</a><img>'
        '<a href="/local">Keep relative link</a><!-- Keep comment --></body></html>'
    )
    assert "SECRET_SCRIPT" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert HtmlActiveContentReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("text", [
    "", "ordinary text", '<P class=keep>Text &amp; café</P>',
    '<img src="images/local.png"><a href="#section">Section</a>',
    '<a href="/page"><input disabled></a>',
    '\ufeff<!DOCTYPE html>\r\n<p>Unicode: café</p>',
])
def test_no_change_returns_exact_original(text, tmp_path):
    artifact = Artifact(data=text.encode("utf-8"))
    result = HtmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=artifact, output_path=tmp_path / "unused.html",
    ))
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("tag,category", [
    ("script", "script"), ("iframe", "embedded_frame"),
    ("object", "embedded_frame"), ("embed", "embedded_frame"),
])
def test_blocked_elements_and_following_content(tag, category):
    content = f'<{tag}>private</{tag}>' if tag != "embed" else '<embed src="private">'
    result = normalize(content + "<p>Keep after element</p>")
    assert result.report.removals == {category: 1}
    assert result.artifact.data == b"<p>Keep after element</p>"


def test_void_embed_and_self_closing_elements_do_not_swallow_content():
    result = normalize('<embed><p>One</p><embed/><p>Two</p><iframe/><p>Three</p>')
    assert result.artifact.data == b"<p>One</p><p>Two</p><p>Three</p>"
    assert result.report.removals == {"embedded_frame": 3}


def test_nested_dropped_elements_and_mismatched_closing():
    result = normalize('<object>private<iframe>private</iframe><embed></object><p>Keep</p>')
    assert result.artifact.data == b"<p>Keep</p>"
    assert result.report.removals == {"embedded_frame": 3}
    mismatched = normalize('<object><iframe>private</object><p>Keep</p>')
    assert mismatched.artifact.data == b"<p>Keep</p>"


@pytest.mark.parametrize("body,removals", [
    ("<object><iframe>private</object>", {"embedded_frame": 2}),
    ("<OBJECT><IFRAME>private</OBJECT>", {"embedded_frame": 2}),
    ("<object><iframe><embed>private</object>", {"embedded_frame": 3}),
    (
        '<object><iframe><script>"</object>"</script></object>',
        {"embedded_frame": 2, "script": 1},
    ),
])
def test_iframe_nesting_recovery_keeps_sanitizing_following_content(body, removals):
    # Newer HTMLParser versions must still deliver the outer closing tag.
    # A closing-tag string inside a script must NOT end suppression early.
    result = normalize(body + '<p onclick="run()">Keep &amp; café</p>')
    assert result.outcome == "changed"
    assert result.artifact.data == "<p>Keep &amp; café</p>".encode("utf-8")
    assert result.report.removals == {**removals, "event_handler": 1}


def test_unclosed_script_discards_remainder_and_can_return_empty_document():
    result = normalize('<script>private<p>still script data')
    assert result.outcome == "changed"
    assert result.artifact.data == b""
    assert result.report.removals == {"script": 1}


@pytest.mark.parametrize("reference", [
    "https://example.invalid", "http://example.invalid", "ftp://example.invalid",
    "file:///etc/passwd", "mailto:example@example.invalid", "javascript:run()",
    "data:text/html,example", "//example.invalid", "  //example.invalid",
    "  JAVASCRIPT:run()", "java&#x73;cript:run()", "java\tscript:run()",
    "java\nscript:run()", "https&#58;//example.invalid", "\\\\example.invalid",
])
@pytest.mark.parametrize("attribute", ["href", "src"])
def test_blocked_references(reference, attribute):
    result = normalize(f'<a {attribute}="{reference}">Keep</a>')
    assert result.artifact.data == b"<a>Keep</a>"
    assert result.report.removals == {"remote_reference": 1}


@pytest.mark.parametrize("reference", ["#section", "/page", "./image.png", "../image.png", "image.png?x=1&y=2", ""])
def test_relative_references_remain(reference):
    text = f'<a href="{reference}">Keep</a>'
    result = normalize(text)
    assert result.outcome == "no_change"
    assert result.artifact.data == text.encode()


def test_case_insensitive_tags_handlers_and_duplicate_attributes():
    result = normalize('<SCRIPT>private</SCRIPT><P ONCLICK="run()" onclick="again()">Keep</P>')
    assert result.artifact.data == b"<p>Keep</p>"
    assert result.report.removals == {"script": 1, "event_handler": 2}


def test_attribute_escaping_boolean_attributes_and_text_entities():
    result = normalize('<input disabled title="&quot; &amp; &lt;" onclick="run()" />Text &amp; &#169;')
    assert result.artifact.data == b'<input disabled title="&quot; &amp; &lt;" />Text &amp; &#169;'


def test_css_and_srcset_are_explicitly_outside_profile():
    text = '<style>body{background:url(https://example.invalid)}</style><img srcset="https://example.invalid 2x">'
    result = normalize(text)
    assert result.outcome == "no_change"
    assert result.artifact.data == text.encode()


@pytest.mark.parametrize("data", [b"\xff", b"<p>caf\xe9</p>", b"<script>x</script>\xc3"])
def test_invalid_utf8_is_failed_without_output(data, tmp_path):
    result = HtmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=data, output_path=tmp_path / "output.html",
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlActiveContentFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_text_normalization_can_prepare_legacy_html():
    prepared = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=b"<p>caf\xe9</p><script>run()</script>", output_path=None,
        declared_encoding="cp1252",
    ))
    result = HtmlActiveContentSanitizer().normalize(NormalizationRequest(source=prepared.artifact, output_path=None))
    assert result.artifact.data == "<p>café</p>".encode()


def test_idempotent_and_shared_settings_ignored():
    worker = HtmlActiveContentSanitizer()
    first = worker.normalize(NormalizationRequest(
        source=b'<p onclick="run()">Keep</p>', output_path=None,
        declared_encoding="ascii", password="unused-secret",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


def test_parser_failure_safe_and_cleans_output(monkeypatch, tmp_path):
    def broken_feed(self, text):
        raise ValueError("private HTML diagnostics")

    monkeypatch.setattr(HtmlRewriter, "feed", broken_feed)
    result = HtmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=b"<script>private</script>", output_path=tmp_path / "output.html",
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlActiveContentFinding.SANITIZATION_FAILED
    assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_invalid_encoder_text_safe_and_cleans_output(monkeypatch, tmp_path):
    def bad_text(self, text):
        self.removals[HtmlActiveContentCategory.SCRIPT] = 1
        self.parts.append("\ud800")

    monkeypatch.setattr(HtmlRewriter, "feed", bad_text)
    result = HtmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=b"<script>private</script>", output_path=tmp_path / "output.html",
    ))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlActiveContentFinding.ENCODE_FAILED
    assert not list(tmp_path.iterdir())


def test_partial_output_error_propagates_and_is_cleaned(monkeypatch, tmp_path):
    worker = HtmlActiveContentSanitizer()
    original = worker._transform
    error = OSError("disk full")

    class BrokenOutput:
        def __init__(self, destination):
            self.destination = destination

        def write(self, data):
            self.destination.write(data[:2])
            raise error

    def wrapped(source, output):
        return original(source, BrokenOutput(output))

    monkeypatch.setattr(worker, "_transform", wrapped)
    with pytest.raises(OSError) as caught:
        worker.normalize(NormalizationRequest(
            source=b"<script>run()</script><p>Keep</p>", output_path=tmp_path / "output.html",
        ))
    assert caught.value is error
    assert not list(tmp_path.iterdir())


def test_short_writes_supported():
    class ShortOutput(BytesIO):
        def write(self, data):
            return super().write(data[:2])

    with BytesIO(b"<script>x</script><p>Keep</p>") as source, ShortOutput() as output:
        report = HtmlActiveContentSanitizer()._transform(source, output)
        assert report.outcome == "changed"
        assert output.getvalue() == b"<p>Keep</p>"


@pytest.mark.parametrize("count", [None, 0, -1, True, "1", 999])
def test_invalid_write_counts_raise(count):
    class InvalidOutput:
        def write(self, data):
            return count

    with BytesIO(b"<script>x</script><p>Keep</p>") as source:
        with pytest.raises(OSError, match="valid number of bytes"):
            HtmlActiveContentSanitizer()._transform(source, InvalidOutput())


def test_existing_destination_preserved(tmp_path):
    destination = tmp_path / "existing.html"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        HtmlActiveContentSanitizer().normalize(NormalizationRequest(
            source=b"<script>x</script>", output_path=destination,
        ))
    assert destination.read_bytes() == b"keep"


def test_worker_contract():
    worker = HtmlActiveContentSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_html_active_content"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


def test_injected_parser_factory_is_used_with_fresh_state_for_each_document():
    instances = []
    received = []

    class RecordingParser(HtmlRewriter):
        def feed(self, text):
            received.append(text)
            super().feed(text)

    def create_parser():
        parser = RecordingParser()
        instances.append(parser)
        return parser

    worker = HtmlActiveContentSanitizer(parser_factory=create_parser)
    assert instances == []
    first = worker.normalize(NormalizationRequest(
        source=b"<script>run()</script><p>First</p>", output_path=None,
    ))
    second = worker.normalize(NormalizationRequest(
        source=b'<p onclick="run()">Second</p>', output_path=None,
    ))
    request = NormalizationRequest(source=b"<p>Third</p>", output_path=None)
    third = worker.normalize(request)

    assert len(instances) == 3
    assert len({id(parser) for parser in instances}) == 3
    assert received == [
        "<script>run()</script><p>First</p>",
        '<p onclick="run()">Second</p>',
        "<p>Third</p>",
    ]
    assert first.artifact.data == b"<p>First</p>"
    assert first.report.removals == {"script": 1}
    assert second.artifact.data == b"<p>Second</p>"
    assert second.report.removals == {"event_handler": 1}
    assert third.outcome == "no_change"
    assert third.artifact is request.source
    assert not third.report.removals


def test_parser_factory_is_not_called_when_utf8_decoding_fails():
    def unexpected_factory():
        raise AssertionError("invalid UTF-8 must not reach the parser")

    worker = HtmlActiveContentSanitizer(parser_factory=unexpected_factory)
    result = worker.normalize(NormalizationRequest(source=b"\xff", output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlActiveContentFinding.DECODE_FAILED


@pytest.mark.parametrize("factory", [None, 1, "parser", HtmlRewriter()])
def test_non_callable_parser_factory_rejected(factory):
    with pytest.raises(TypeError, match="parser_factory must be callable"):
        HtmlActiveContentSanitizer(parser_factory=factory)


@pytest.mark.parametrize("removals", [{"script": 0}, {"script": -1}, {"script": True}, {"script": "1"}, {"unknown": 1}])
def test_invalid_counts_rejected(removals):
    with pytest.raises(ValidationError):
        HtmlActiveContentReport(outcome="changed", removals=removals)


def test_immutable_report_and_json_roundtrip():
    counts = {"script": 1}
    report = HtmlActiveContentReport(outcome="changed", removals=counts)
    counts["script"] = 99
    assert report.removals == {"script": 1}
    with pytest.raises(TypeError):
        report.removals["script"] = 2
    with pytest.raises(ValidationError):
        report.outcome = "failed"
    assert HtmlActiveContentReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        HtmlActiveContentIssue(code=HtmlActiveContentFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "changed"},
    {"outcome": "no_change", "removals": {"script": 1}},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        HtmlActiveContentReport(**kwargs)
