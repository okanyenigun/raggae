"""Parsed Markdown shares the reader contract without source mutation or policies."""

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
from raggae.documents.text_reader import MarkdownTextReader, TextReader, TextReadResult


@pytest.fixture
def backend():
    return pytest.importorskip("markdown_it", reason="Install raggae[text-markdown]")


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
def test_sources_and_common_result_shape(backend, tmp_path, kind, keyword):
    data = "# Café İstanbul\r\n\r\nPay **90** USD.\r\n".encode("utf-8")
    path = tmp_path / "notes.md"
    path.write_bytes(data)
    sources = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }
    reader = MarkdownTextReader()
    source = sources[kind]
    result = reader.read(source=source) if keyword else reader.read(source)
    assert isinstance(reader, TextReader)
    assert result.reader == reader.name == "text_reader_markdown"
    assert result.outcome == "success" and result.findings == ()
    assert [b.text for b in result.blocks] == ["Café İstanbul", "Pay 90 USD."]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.content_format == "plain_text" and block.method == "native"
        assert "location" not in block.model_dump()
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    assert "private-password" not in result.model_dump_json()
    if isinstance(source, Artifact):
        assert source.password == "private-password"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Title\n=====\n\n## Subheading", ["Title", "Subheading"]),
        (
            "**Bold** and *soft* and ~~old~~; `x < y`.\nNext  \nHard break.",
            ["Bold and soft and old; x < y.\nNext\nHard break."],
        ),
        (
            "- First\n- Second\n  - Nested\n\n1. Third",
            ["First", "Second", "Nested", "Third"],
        ),
        ("> Quoted **words**.\n>\n> More words.", ["Quoted words.", "More words."]),
        ("One.\n\nOne.", ["One.", "One."]),
        (r"Escaped \*stars\* &amp; &lt;words&gt;.", ["Escaped *stars* & <words>."]),
        ("[Pay here][pay]\n\n[pay]: https://example.invalid/payment", ["Pay here"]),
        ("<https://example.invalid/path>", ["https://example.invalid/path"]),
        (
            "Before ![A **red** chart](https://example.invalid/image.png) after.",
            ["Before A red chart after."],
        ),
        ("    if ready:\n        run()\n", ["if ready:\n    run()\n"]),
        (
            "```python\nraise RuntimeError('do not run')\n```",
            ["raise RuntimeError('do not run')\n"],
        ),
        (
            "| Item | Cost |\n| --- | --- |\n| Labels | 90 |",
            ["Item", "Cost", "Labels", "90"],
        ),
    ],
)
def test_visible_prose_code_and_table_cells(backend, source, expected):
    result = MarkdownTextReader().read(source.encode("utf-8"))
    assert result.outcome == "success" and result.findings == ()
    assert [b.text for b in result.blocks] == expected


@pytest.mark.parametrize(
    "source", ["", " \r\n\t", "---", "![](https://example.invalid/image.png)"]
)
def test_empty_extractable_text_is_explicit(backend, source):
    result = MarkdownTextReader().read(source.encode())
    assert result.outcome == "success" and result.blocks == ()
    (finding,) = result.findings
    assert finding.code == "empty" and finding.page_index == 0


@pytest.mark.parametrize(
    "html", ["<script>private-script()</script>", "Text <b>private-text</b>."]
)
@pytest.mark.parametrize("with_supported_content", [False, True])
def test_raw_html_is_explicitly_omitted_not_silently_complete(
    backend, html, with_supported_content
):
    source = ("# Kept\n\n" if with_supported_content else "") + html
    result = MarkdownTextReader().read(source.encode())
    assert result.outcome == ("partial" if with_supported_content else "failed")
    assert [b.text for b in result.blocks] == (
        ["Kept"] if with_supported_content else []
    )
    (finding,) = result.findings
    assert finding.code == "html_omitted" and finding.page_index == 0
    assert "private-" not in result.model_dump_json()


def test_html_in_code_is_literal_code_not_omitted_markup(backend):
    result = MarkdownTextReader().read(
        b"`<b>literal</b>`\n\n```html\n<script>example()</script>\n```"
    )
    assert result.outcome == "success" and not result.findings
    assert [b.text for b in result.blocks] == [
        "<b>literal</b>",
        "<script>example()</script>\n",
    ]


def test_link_and_image_urls_are_not_fetched(backend, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Reader must not access the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = MarkdownTextReader().read(
        b"[Read the manual](https://example.invalid/manual)\n\n![Chart](https://example.invalid/chart.png)"
    )
    assert result.outcome == "success"
    assert [b.text for b in result.blocks] == ["Read the manual", "Chart"]


@pytest.mark.parametrize(
    "encoding,text",
    [
        ("utf-8", "Café 🌍"),
        ("utf-8-sig", "Café 🌍"),
        ("utf-16", "Café 🌍"),
        ("utf-32", "Café 🌍"),
        ("cp1252", "Café €"),
        ("iso-8859-1", "Café"),
        ("ascii", "Plain"),
    ],
)
def test_explicit_encoding_is_used_strictly(backend, encoding, text):
    result = MarkdownTextReader(encoding=encoding).read(("# " + text).encode(encoding))
    assert result.outcome == "success" and result.blocks[0].text == text


@pytest.mark.parametrize("data", [b"# Caf\xe9", b"\xff", b"# valid\n\ninvalid\xff"])
def test_invalid_encoding_has_no_guessed_or_partial_text(data):
    result = MarkdownTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "decode_failed"
    assert result.findings[0].page_index is None


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
        MarkdownTextReader(encoding=encoding)


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable", "pipe"])
def test_unreadable_sources_return_safe_findings(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-file.md"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_bytes(b"# Hello")

        def denied(*args, **kwargs):
            raise PermissionError("private parser detail")

        monkeypatch.setattr(Path, "open", denied)
    elif kind == "pipe":
        monkeypatch.setattr(
            Path, "stat", lambda *args, **kwargs: SimpleNamespace(st_mode=S_IFIFO)
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("Pipe must not be opened")

        monkeypatch.setattr(Path, "open", forbidden)
    result = MarkdownTextReader().read(Artifact(path=path, password="private-password"))
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "source_unreadable"
    assert "private" not in result.model_dump_json()


def test_file_is_read_completely_and_handle_is_closed(backend, tmp_path, monkeypatch):
    path = tmp_path / "source.bin"
    path.write_bytes(b"x")
    stream = BytesIO(b"# Complete")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=S_IFREG, st_size=60 * 1024 * 1024
        ),
    )
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    result = MarkdownTextReader().read(path)
    assert result.outcome == "success" and result.blocks[0].text == "Complete"
    assert stream.closed


def test_text_beyond_former_reader_limits_is_not_truncated(backend):
    code = "x" * (10 * 1024 * 1024) + "\n"
    result = MarkdownTextReader().read(("```\n" + code + "```\n").encode())
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == code
    assert not hasattr(MarkdownTextReader(), "policy")


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_region_requests_reject_without_io_or_whole_document_fallback(
    tmp_path, monkeypatch, layout, selection
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported selection must not access source or parser")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = MarkdownTextReader().read(
        tmp_path / "missing.md", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert all(f.code == "selection_unsupported" for f in result.findings)


@pytest.mark.parametrize("selection", [(), []])
def test_a_layout_is_refused_without_io_even_when_nothing_is_selected(
    tmp_path, layout, selection
):
    result = MarkdownTextReader().read(
        tmp_path / "missing.md", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


def test_a_map_without_regions_is_refused_too(tmp_path, layout):
    empty = LayoutAnalysisResult(outcome="success", surfaces=layout.surfaces)
    result = MarkdownTextReader().read(tmp_path / "missing.md", layout=empty)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize(
    "ids,error",
    [
        ("first", TypeError),
        (42, TypeError),
        ([None], TypeError),
        (("first", "first"), ValueError),
        (("missing",), ValueError),
    ],
)
def test_invalid_selection_is_a_caller_error(layout, ids, error):
    with pytest.raises(error):
        MarkdownTextReader().read(b"# Hello", layout=layout, region_ids=ids)


def test_ids_require_layout_and_unusable_layout_is_not_read():
    with pytest.raises(ValueError, match="require a layout"):
        MarkdownTextReader().read(b"# Hello", region_ids=())
    unusable = LayoutAnalysisResult(
        outcome="failed", findings=(LayoutFinding(code="failed", message="No map."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        MarkdownTextReader().read(b"# Hello", layout=unusable)
    with pytest.raises(TypeError, match="layout must be"):
        MarkdownTextReader().read(b"# Hello", layout={})


@pytest.mark.parametrize(
    "source", [None, 12, bytearray(b"# Hello"), BytesIO(b"# Hello")]
)
def test_unsupported_source_types_raise(source):
    with pytest.raises(TypeError, match="source must be"):
        MarkdownTextReader().read(source)


def test_missing_backend_has_an_install_instruction(monkeypatch):
    original = builtins.__import__

    def without_backend(name, *args, **kwargs):
        if name == "markdown_it":
            raise ImportError("deliberately absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_backend)
    with pytest.raises(ImportError, match=r"raggae\[text-markdown\]"):
        MarkdownTextReader().read(b"# Hello")


@pytest.mark.parametrize(
    "error", [ValueError("private details"), RecursionError("private details")]
)
def test_parser_failures_do_not_leak_source_or_return_partial_text(
    backend, monkeypatch, error
):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(backend.MarkdownIt, "parse", broken)
    result = MarkdownTextReader().read(b"# Heading")
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "parse_failed"
    assert "private" not in result.model_dump_json()


def test_unexpected_programming_errors_are_not_hidden(backend, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(backend.MarkdownIt, "parse", broken)
    with pytest.raises(RuntimeError, match="unexpected"):
        MarkdownTextReader().read(b"# Hello")
