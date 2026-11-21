"""Literal Markdown is returned unchanged through the shared reader contract."""

import builtins
from io import BytesIO
from pathlib import Path
from stat import S_IFIFO, S_IFREG
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    RawMarkdownTextReader,
    TextReader,
    TextReadResult,
)


@pytest.fixture
def layout():
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=300,
                height=400,
            ),
        ),
        regions=(
            LayoutRegion(
                id="first", surface_id="page-1", kind="text", bbox=(10, 10, 100, 40)
            ),
            LayoutRegion(
                id="second", surface_id="page-1", kind="text", bbox=(10, 50, 100, 80)
            ),
        ),
    )


@pytest.mark.parametrize(
    "kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_source_forms_preserve_exact_markup_and_common_result(tmp_path, kind, keyword):
    text = '# Café İstanbul\r\n\r\n  **Pay** [here](https://example.invalid).\t\r\n\n```py\nprint("ready")\n```\r'
    data = text.encode("utf-8")
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[kind]
    reader = RawMarkdownTextReader()
    result = reader.read(source=source) if keyword else reader.read(source)
    assert isinstance(reader, TextReader)
    assert result.reader == reader.name == "text_reader_raw_markdown"
    assert result.outcome == "success" and not result.findings
    (block,) = result.blocks
    assert block.text == text
    assert block.page_index == 0 and block.region_id is None
    assert block.content_format == "markdown" and block.method == "native"
    assert set(block.model_dump()) == {
        "text",
        "page_index",
        "region_id",
        "content_format",
        "method",
    }
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    assert "private-password" not in result.model_dump_json()
    if isinstance(source, Artifact):
        assert source.password == "private-password"


def test_does_not_parse_render_sanitize_or_fetch_source(monkeypatch):
    import socket

    original = builtins.__import__

    def no_parser(name, *args, **kwargs):
        if name.split(".")[0] in {"markdown_it", "requests", "httpx"}:
            raise AssertionError("Raw reader must not load a parser or HTTP client")
        return original(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("Raw reader must not access the network")

    monkeypatch.setattr(builtins, "__import__", no_parser)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    text = '# Title\n\n<script src="https://example.invalid/remote.js"></script>\n\n![Chart](https://example.invalid/image.png)\n\n```python\nraise RuntimeError("never execute")\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n'
    result = RawMarkdownTextReader().read(text.encode())
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == text


@pytest.mark.parametrize(
    "encoding,text",
    [
        ("utf-8", "# Café 🌍\n"),
        ("utf-8-sig", "# Café 🌍\n"),
        ("utf-16", "# Café 🌍\n"),
        ("utf-32", "# Café 🌍\n"),
        ("cp1252", "# Café €\r\n"),
        ("iso-8859-1", "# Café\n"),
        ("ascii", "# Plain\n"),
    ],
)
def test_explicit_encoding(encoding, text):
    result = RawMarkdownTextReader(encoding=encoding).read(text.encode(encoding))
    assert result.outcome == "success" and result.blocks[0].text == text


def test_bom_is_preserved_unless_utf8_sig_is_selected():
    data = "# Title\n".encode("utf-8-sig")
    assert RawMarkdownTextReader().read(data).blocks[0].text == "\ufeff# Title\n"
    assert (
        RawMarkdownTextReader(encoding="utf-8-sig").read(data).blocks[0].text
        == "# Title\n"
    )


@pytest.mark.parametrize("data", [b"# Caf\xe9", b"\xff", b"# Good prefix\n\xff"])
def test_decoding_failure_has_no_guessed_or_partial_text(data):
    result = RawMarkdownTextReader().read(data)
    assert result.outcome == "failed" and result.blocks == ()
    assert result.findings[0].code == "decode_failed"
    assert result.findings[0].page_index is None


@pytest.mark.parametrize(
    "data,encoding", [(b"", "utf-8"), (b"\xef\xbb\xbf", "utf-8-sig")]
)
def test_empty_source_is_explicit(data, encoding):
    result = RawMarkdownTextReader(encoding=encoding).read(data)
    assert result.outcome == "success" and result.blocks == ()
    assert result.findings[0].code == "empty"
    assert result.findings[0].page_index == 0


@pytest.mark.parametrize("text", [" ", "\r\n\n\t", "---\n", "#\n", "\x00"])
def test_nonempty_source_is_not_stripped_or_interpreted(text):
    result = RawMarkdownTextReader().read(text.encode())
    assert result.outcome == "success" and not result.findings
    assert len(result.blocks) == 1 and result.blocks[0].text == text


@pytest.mark.parametrize("file_backed", [False, True])
def test_complete_source_beyond_previous_limits(tmp_path, file_backed):
    text = "# Heading\r\n" + "x" * (10 * 1024 * 1024) + " 🌍\n"
    data = text.encode()
    source = data
    if file_backed:
        source = tmp_path / "large.md"
        source.write_bytes(data)
    result = RawMarkdownTextReader().read(source)
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == text
    assert not hasattr(RawMarkdownTextReader(), "policy")


def test_file_read_is_complete_and_closes_its_handle(tmp_path, monkeypatch):
    path = tmp_path / "source.md"
    path.write_bytes(b"x")
    sizes = []

    class RecordedStream(BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    stream = RecordedStream(b"# Full source\r\n")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=S_IFREG, st_size=60 * 1024 * 1024
        ),
    )
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    result = RawMarkdownTextReader().read(path)
    assert result.outcome == "success" and result.blocks[0].text == "# Full source\r\n"
    assert sizes == [-1] and stream.closed


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable", "pipe"])
def test_unreadable_sources_are_safe(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-file.md"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_bytes(b"# Hello")

        def denied(*args, **kwargs):
            raise PermissionError("private error detail")

        monkeypatch.setattr(Path, "open", denied)
    elif kind == "pipe":
        monkeypatch.setattr(
            Path, "stat", lambda *args, **kwargs: SimpleNamespace(st_mode=S_IFIFO)
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("Must not open a pipe")

        monkeypatch.setattr(Path, "open", forbidden)
    result = RawMarkdownTextReader().read(
        Artifact(path=path, password="private-password")
    )
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "source_unreadable"
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_region_requests_reject_without_source_io(
    tmp_path, monkeypatch, layout, selection
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported selection must not read source")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = RawMarkdownTextReader().read(
        tmp_path / "missing.md", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert all(f.code == "selection_unsupported" for f in result.findings)


@pytest.mark.parametrize("selection", [(), []])
def test_a_layout_is_refused_even_when_nothing_is_selected(tmp_path, layout, selection):
    result = RawMarkdownTextReader().read(
        tmp_path / "missing.md", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


def test_a_map_without_regions_is_refused_too(tmp_path, layout):
    empty = LayoutAnalysisResult(outcome="success", surfaces=layout.surfaces)
    result = RawMarkdownTextReader().read(tmp_path / "missing.md", layout=empty)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize(
    "ids,error",
    [
        ("first", TypeError),
        ([None], TypeError),
        (42, TypeError),
        (("first", "first"), ValueError),
        (("missing",), ValueError),
    ],
)
def test_invalid_selection_is_a_caller_error(layout, ids, error):
    with pytest.raises(error):
        RawMarkdownTextReader().read(b"# Hello", layout=layout, region_ids=ids)


def test_missing_or_unusable_layout_is_invalid():
    reader = RawMarkdownTextReader()
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(b"# Hello", region_ids=())
    with pytest.raises(TypeError, match="layout must be"):
        reader.read(b"# Hello", layout={})
    unusable = LayoutAnalysisResult(
        outcome="failed", findings=(LayoutFinding(code="failed", message="No map."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        reader.read(b"# Hello", layout=unusable)


@pytest.mark.parametrize(
    "source", [None, 12, bytearray(b"# Hello"), BytesIO(b"# Hello")]
)
def test_unsupported_sources_are_rejected(source):
    with pytest.raises(TypeError, match="source must be"):
        RawMarkdownTextReader().read(source)


@pytest.mark.parametrize(
    "encoding,error",
    [
        ("unknown", ValueError),
        ("base64_codec", ValueError),
        ("", ValueError),
        ("UTF-8", ValueError),
        (None, TypeError),
        (12, TypeError),
        (b"utf-8", TypeError),
    ],
)
def test_invalid_encoding_is_a_configuration_error(encoding, error):
    with pytest.raises(error, match="encoding must be"):
        RawMarkdownTextReader(encoding=encoding)
