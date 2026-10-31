"""Exact tag removal, retained encoding/content, safe failures, and shared API."""

from io import BytesIO

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    HtmlMetadataFinding,
    HtmlMetadataIssue,
    NormalizationWorker,
    HtmlMetadataReport,
    HtmlMetadataSanitizer,
    NormalizationRequest,
    NormalizationResult,
    StrictTextEncodingNormalizer,
)
from raggae.documents.normalization.sanitization.html_metadata.parser import MetadataParser


@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_all_standard_names_removed_and_other_bytes_preserved(source_kind, disk, tmp_path):
    tags = [f'<meta name="{name}" content="Private value">' for name in ["author", "creator", "description", "generator", "keywords"]]
    text = (
        '<!DOCTYPE html>\r\n<HTML><head><meta charset="utf-8">\r\n'
        + "\r\n".join(tags) + '<meta name="viewport" content="width=device-width">'
        '</head><body><P class=keep>Café &amp; &#169;</P><!-- Keep comment -->'
        '<script>app.run()</script></body></HTML>'
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
    result = HtmlMetadataSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.removed_tags == 5
    assert result.report.issues == ()
    expected = text
    for tag in tags:
        expected = expected.replace(tag, "")
    assert result.artifact.read_bytes() == expected.encode("utf-8")
    assert result.artifact.path == destination
    assert request.source.read_bytes() == original
    assert "Private value" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert HtmlMetadataReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("name", ["author", "creator", "description", "generator", "keywords"])
@pytest.mark.parametrize("attribute", ["name", "property"])
def test_each_name_or_property_selected(name, attribute):
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(
        source=f'<meta {attribute}="{name}" content="private"><p>Keep</p>'.encode(), output_path=None,
    ))
    assert result.report.removed_tags == 1
    assert result.artifact.data == b"<p>Keep</p>"


@pytest.mark.parametrize("tag", [
    '<META NAME="AUTHOR" CONTENT="private">', '<meta name=author content=private>',
    '<meta name=" author " content="private" />', '<meta name="auth&#111;r">',
    '<meta name="viewport" name="author">', '<meta name="author" property="creator">',
    '<meta name="author" content="contains > inside quotes">',
])
def test_case_entities_quoting_duplicates_and_selfclosing(tag):
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=(tag + "Keep").encode(), output_path=None))
    assert result.report.removed_tags == 1
    assert result.artifact.data == b"Keep"


@pytest.mark.parametrize("tag", [
    '<meta charset="utf-8">', '<META CHARSET=UTF-8 />',
    '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">',
    '<meta charset="utf-8" name="author" content="mixed tag">',
    '<meta name="author" http-equiv=" Content-Type " content="text/html; charset=utf-8">',
])
def test_encoding_declarations_preserved_even_on_changed_documents(tag):
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(
        source=(tag + '<meta name="author" content="private"><p>Keep</p>').encode(), output_path=None,
    ))
    assert result.report.removed_tags == 1
    assert result.artifact.data == (tag + "<p>Keep</p>").encode()


@pytest.mark.parametrize("text", [
    "", "ordinary text", '<META CHARSET=UTF-8><P class=keep>Café &amp; text</P>',
    '<meta name="viewport" content="width=device-width"><meta property="og:title" content="Keep">',
    '<!-- <meta name="author"> --><script>const s = \'<meta name="author">\';</script>',
    '\ufeff<!DOCTYPE html>\r\n<p>Keep author in visible text</p>',
])
def test_exact_noops_return_original(text, tmp_path):
    artifact = Artifact(data=text.encode("utf-8"))
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=artifact, output_path=tmp_path / "unused.html"))
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert result.report.removed_tags == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("prefix", ["Café\r\n", "Unicode: İ \n", "\ufeffbefore\r", "\n\n\n"])
def test_exact_offsets_with_unicode_and_line_breaks(prefix):
    tag = '<meta\n name="author"\r\n content="private">'
    suffix = "\r\nKeep &amp; after"
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=(prefix + tag + suffix).encode(), output_path=None))
    assert result.artifact.data == (prefix + suffix).encode()


def test_removed_tag_can_leave_empty_output():
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=b'<meta name="author">', output_path=None))
    assert result.outcome == "changed"
    assert result.artifact.data == b""


def test_scripts_and_other_private_content_not_removed():
    keep = '<title>Private title</title><p>Author: Jane</p><!-- Jane --><script>run()</script><meta property="og:description" content="Keep">'
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(
        source=(keep + '<meta name="description" content="Remove">').encode(), output_path=None,
    ))
    assert result.artifact.data == keep.encode()


@pytest.mark.parametrize("data", [b"\xff", b'<meta name="author">caf\xe9', b'<meta name="author">\xc3'])
def test_invalid_utf8_has_no_output(data, tmp_path):
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output.html"))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlMetadataFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_text_worker_can_prepare_legacy_html():
    prepared = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=b'<meta name="author"><p>caf\xe9</p>', output_path=None, declared_encoding="cp1252",
    ))
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=prepared.artifact, output_path=None))
    assert result.artifact.data == "<p>café</p>".encode()


def test_idempotent_and_shared_settings_ignored():
    worker = HtmlMetadataSanitizer()
    first = worker.normalize(NormalizationRequest(
        source=Artifact(data=b'<meta name="author"><p>Keep</p>', password="unused-secret"), output_path=None,
         declared_encoding="ascii",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


@pytest.mark.parametrize("error", [ValueError("private parser diagnostics"), AssertionError("private parser diagnostics")])
def test_parser_error_safe_and_output_cleaned(error, monkeypatch, tmp_path):
    def broken_feed(self, text):
        raise error

    monkeypatch.setattr(MetadataParser, "feed", broken_feed)
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source=b'<meta name="author">', output_path=tmp_path / "output.html"))
    assert result.artifact is None
    assert result.report.issues[0].code == HtmlMetadataFinding.SANITIZATION_FAILED
    assert result.report.removed_tags == 0
    assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_partial_output_failure_propagates_after_cleanup(monkeypatch, tmp_path):
    worker = HtmlMetadataSanitizer()
    transform = worker._transform
    error = OSError("disk full")

    class BrokenOutput:
        def __init__(self, output):
            self.output = output

        def write(self, data):
            self.output.write(data[:2])
            raise error

    def wrapped(source, output):
        return transform(source, BrokenOutput(output))

    monkeypatch.setattr(worker, "_transform", wrapped)
    with pytest.raises(OSError) as caught:
        worker.normalize(NormalizationRequest(source=b'<meta name="author"><p>Keep</p>', output_path=tmp_path / "output.html"))
    assert caught.value is error
    assert not list(tmp_path.iterdir())


def test_short_writes_supported():
    class ShortOutput(BytesIO):
        def write(self, data):
            return super().write(data[:2])

    with BytesIO(b'<meta name="author"><p>Keep</p>') as source, ShortOutput() as output:
        report = HtmlMetadataSanitizer()._transform(source, output)
        assert report.removed_tags == 1
        assert output.getvalue() == b"<p>Keep</p>"


@pytest.mark.parametrize("count", [None, 0, -1, True, "1", 999])
def test_invalid_write_counts_raise(count):
    class InvalidOutput:
        def write(self, data):
            return count

    with BytesIO(b'<meta name="author"><p>Keep</p>') as source:
        with pytest.raises(OSError, match="valid number of bytes"):
            HtmlMetadataSanitizer()._transform(source, InvalidOutput())


def test_existing_destination_never_overwritten(tmp_path):
    path = tmp_path / "existing.html"
    original = b'<meta name="author">'
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        HtmlMetadataSanitizer().normalize(NormalizationRequest(source=path, output_path=path))
    assert path.read_bytes() == original


def test_worker_contract():
    worker = HtmlMetadataSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_html_metadata"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


@pytest.mark.parametrize("count", [-1, True, "1"])
def test_invalid_report_count_rejected(count):
    with pytest.raises(ValidationError):
        HtmlMetadataReport(outcome="changed", removed_tags=count)


def test_report_read_only_and_json_serializable():
    report = HtmlMetadataReport(outcome="changed", removed_tags=2)
    assert HtmlMetadataReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.removed_tags = 3
    with pytest.raises(ValidationError):
        HtmlMetadataIssue(code=HtmlMetadataFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "changed"},
    {"outcome": "no_change", "removed_tags": 1},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        HtmlMetadataReport(**kwargs)
