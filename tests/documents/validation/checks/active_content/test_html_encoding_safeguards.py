import codecs
import errno
from pathlib import Path

import pytest

from raggae.documents.validation import ActiveContentPolicy, HtmlActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


ENCODINGS = [("utf-8", codecs.BOM_UTF8, "utf-8-sig"),
             ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16"),
             ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16")]


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
@pytest.mark.parametrize("content,codes", [
    ("", ()),
    ('<p>Résumé 中文 😀</p>', ()),
    ('<!-- <script src="https://evil.test/file"></script> -->', ()),
    ('<p>😀</p><iframe src="//evil.test/frame"></iframe><button onclick="test()">x</button>',
     (Code.EMBEDDED_FRAME, Code.EVENT_HANDLER, Code.REMOTE_REFERENCE)),
])
def test_bom_decoding_preserves_context_and_unicode(file_factory, encoding, bom, codec, content, codes):
    data = bom + content.encode(encoding)
    assert data.decode(codec) == content
    path = file_factory("unicode.html", data)
    result = HtmlActiveContentProbe().check(path, "html")
    assert result.codes == codes
    assert dict(result.facts) == {}
    if Code.REMOTE_REFERENCE in codes:
        assert dict(result.findings[-1].detail) == {"count": 1, "sample": "https://evil.test/frame"}
    assert path.read_bytes() == data


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
def test_bom_selects_expected_text_codec(file_factory, monkeypatch, encoding, bom, codec):
    path = file_factory("codec.html", bom + '<script>test()</script>'.encode(encoding))
    original_read = Path.read_text
    calls = []

    def read_text(target, *args, **kwargs):
        calls.append((target, args, kwargs))
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert HtmlActiveContentProbe().check(path, "html").codes == (Code.SCRIPT,)
    assert calls == [(path, (), {"encoding": codec, "errors": "replace"})]


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
def test_decoded_allowlisted_reference_does_not_hide_script(file_factory, encoding, bom, codec):
    content = '<script src="https://cdn.example.com/file"></script>'
    path = file_factory("allowed.html", bom + content.encode(encoding))
    probe = HtmlActiveContentProbe(ActiveContentPolicy(allowed_reference_hosts={"example.com"}))
    assert probe.check(path, "html").codes == (Code.SCRIPT,)


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", codecs.BOM_UTF16_LE),
                                         ("utf-16-be", codecs.BOM_UTF16_BE)])
@pytest.mark.parametrize("suffix", [b"\x01", b"\x00\xd8\xd8\x00"])
def test_utf16_replacement_does_not_hide_prior_script(file_factory, encoding, bom, suffix):
    data = bom + '<script>test()</script>'.encode(encoding) + suffix
    path = file_factory("damaged.html", data)
    assert HtmlActiveContentProbe().check(path, "html").codes == (Code.SCRIPT,)
    assert path.read_bytes() == data


@pytest.mark.parametrize("phase", ["prefix", "text"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, IsADirectoryError])
def test_errors_at_either_read_phase_are_unreadable(file_factory, monkeypatch, phase, error_type):
    path = file_factory("read-error.html", codecs.BOM_UTF16_LE + '<script/>'.encode("utf-16-le"))
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise error_type(errno.EIO, "cannot read HTML")

    monkeypatch.setattr(Path, "open" if phase == "prefix" else "read_text", fail)
    result = HtmlActiveContentProbe().check(path, "html")
    assert calls == [True]
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": path.name}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("format_name", ["pdf", "csv", "htm", " .HTM "])
def test_skipped_format_does_not_even_sniff_bom(monkeypatch, missing_path, forbidden_operation, format_name):
    monkeypatch.setattr(Path, "open", forbidden_operation)
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    assert HtmlActiveContentProbe().check(missing_path, format_name).codes == (Code.NOT_APPLICABLE,)


def test_encoding_selection_is_per_file(file_factory):
    probe = HtmlActiveContentProbe()
    for index, (encoding, bom, _) in enumerate(ENCODINGS):
        path = file_factory(f"script-{index}.html", bom + '<script/>'.encode(encoding))
        assert probe.check(path, "html").codes == (Code.SCRIPT,)
    path = file_factory("plain.html", b"<p>plain UTF-8 without BOM</p>")
    assert probe.check(path, "html").codes == ()
