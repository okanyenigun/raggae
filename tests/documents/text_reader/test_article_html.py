"""A page's main article as one Markdown block, via trafilatura, offline."""

import builtins
import socket
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis.region_discovery.html import HtmlLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    ArticleHtmlTextReader,
    TextReader,
    TextReadResult,
)

pytest.importorskip("trafilatura", reason="Install raggae[text-html]")

PAGE = """<html><head><title>Shop title</title><style>p { color: red }</style>
<script>trackVisit()</script></head><body>
<nav><a href="/">Home</a> | <a href="/shop">Shop</a> | <a href="/cart">Cart</a></nav>
<article>
<h1>Motor X200 review</h1>
<p>The X200 is a quiet motor with a <b>two-year</b> warranty. It ships with mounting
brackets and a <a href="https://example.com/manual">detailed manual</a> for installers.</p>
<h2>Specifications</h2>
<ul><li>Torque: 12 Nm</li><li>Speed: 3000 rpm</li></ul>
<table><tr><th>Part</th><th>Years</th></tr><tr><td>Motor</td><td>2</td></tr></table>
<p><img src="https://example.com/motor.png" alt="Motor photo"> Overall, a reliable
choice for workshops that need steady power over long shifts and low maintenance.</p>
</article>
<section class="comments"><h3>Comments</h3>
<p>Great review, thanks! I bought one last week and it runs very quietly.</p></section>
<footer>Copyright 2026 Example Shop. All rights reserved.</footer>
</body></html>"""

SHORT = """<html><body><nav><a href="/">Home</a> <a href="/shop">Shop</a></nav>
<article><h1>Motor X200 review</h1><p>The X200 is a quiet motor with a <b>two-year</b> warranty.</p>
<ul><li>Torque: 12 Nm</li></ul></article>
<footer>Copyright 2026</footer></body></html>"""


def read(markup, **kwargs):
    if isinstance(markup, str):
        markup = markup.encode()
    options = {key: kwargs.pop(key) for key in ["focus", "encoding"] if key in kwargs}
    return ArticleHtmlTextReader(**options).read(markup, **kwargs)


def codes(result):
    return [finding.code for finding in result.findings]


def test_html_family_exports_both_readers():
    from raggae.documents.text_reader import html as html_readers

    assert html_readers.ArticleHtmlTextReader is ArticleHtmlTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = PAGE.encode()
    path = tmp_path / "review.html"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = ArticleHtmlTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_article_html"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.content_format == "markdown" and block.method == "native"
    assert block.page_index == 0 and block.region_id is None
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_main_article_is_one_markdown_block_without_page_chrome():
    text = read(PAGE).blocks[0].text
    assert text.startswith("# Motor X200 review")
    for kept in (
        "**two-year**",
        "## Specifications",
        "- Torque: 12 Nm",
        "| Part | Years |",
        "| Motor | 2 |",
        "detailed manual",
        "low maintenance.",
    ):
        assert kept in text
    for dropped in (
        "Home",
        "Cart",
        "Copyright",
        "Great review",
        "Comments",
        "Shop title",
        "trackVisit",
        "color: red",
        "https://example.com",
        "motor.png",
    ):
        assert dropped not in text


@pytest.mark.parametrize("focus", ["balanced", "precision", "recall"])
def test_short_articles_keep_their_structure_in_every_focus(focus):
    # Agreed: a minimum extracted size of 1 keeps trafilatura's structured result.
    text = read(SHORT, focus=focus).blocks[0].text
    assert text == (
        "# Motor X200 review\n\n"
        "The X200 is a quiet motor with a **two-year** warranty.\n\n"
        "- Torque: 12 Nm"
    )


def test_a_line_break_inside_a_source_paragraph_stays_a_markdown_soft_break():
    page = SHORT.replace("quiet motor with a ", "quiet motor with a\n")
    text = read(page).blocks[0].text
    assert "The X200 is a quiet motor with a\n**two-year** warranty." in text


def test_extraction_options_and_private_config(monkeypatch):
    import trafilatura
    from trafilatura.settings import DEFAULT_CONFIG

    calls = []
    real = trafilatura.extract

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(trafilatura, "extract", spy)
    for focus in ("balanced", "precision", "recall"):
        read(SHORT, focus=focus)
    assert [(c["favor_precision"], c["favor_recall"]) for c in calls] == [
        (False, False),
        (True, False),
        (False, True),
    ]
    for call in calls:
        assert call["output_format"] == "markdown"
        assert call["include_comments"] is False
        assert call["include_links"] is False and call["include_images"] is False
        assert call["with_metadata"] is False
        assert call["config"].getint("DEFAULT", "MIN_EXTRACTED_SIZE") == 1
        assert call["config"] is not DEFAULT_CONFIG
    assert DEFAULT_CONFIG.getint("DEFAULT", "MIN_EXTRACTED_SIZE") == 250


def test_output_is_deterministic():
    assert read(PAGE) == read(PAGE)


@pytest.mark.parametrize(
    "markup",
    [
        "",
        "   ",
        "<html><body></body></html>",
        "<html><body><script>x()</script></body></html>",
    ],
)
def test_no_article_succeeds_with_an_explanation(markup):
    result = read(markup)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]
    assert result.findings[0].page_index == 0


def test_never_fetches_or_downloads(monkeypatch):
    import trafilatura

    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(trafilatura, "fetch_url", no_network)
    assert read(PAGE).outcome == "success"


def test_backend_parse_errors_fail_safely(monkeypatch):
    import trafilatura

    def broken(*args, **kwargs):
        raise ValueError("private backend detail")

    monkeypatch.setattr(trafilatura, "extract", broken)
    result = read(PAGE)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["parse_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_backend_has_an_install_hint(monkeypatch):
    real_import = builtins.__import__

    def without_trafilatura(name, *args, **kwargs):
        if name == "trafilatura" or name.startswith("trafilatura."):
            raise ImportError("no trafilatura")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_trafilatura)
    with pytest.raises(ImportError, match=r"raggae\[text-html\]"):
        read(PAGE)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1252"])
def test_declared_encodings(encoding):
    page = SHORT.replace("quiet motor", "quiet café motor")
    result = ArticleHtmlTextReader(encoding=encoding).read(page.encode(encoding))
    assert "quiet café motor" in result.blocks[0].text


def test_decode_failure_is_private():
    result = read(b"<p>private\xff</p>")
    assert result.outcome == "failed"
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = ArticleHtmlTextReader()
    result = reader.read(tmp_path / "private-name.html")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"focus": None}, TypeError),
        ({"focus": "fast"}, ValueError),
        ({"encoding": "unknown-codec"}, ValueError),
    ],
)
def test_configuration_errors(kwargs, error):
    with pytest.raises(error):
        ArticleHtmlTextReader(**kwargs)


def test_layout_regions_are_rejected_before_source_io(tmp_path, monkeypatch):
    layout = HtmlLayoutAnalyzer().analyze(PAGE.encode())
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    reader = ArticleHtmlTextReader()
    result = reader.read(tmp_path / "missing.html", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.code for f in result.findings} == {"selection_unsupported"}
    assert [f.region_id for f in result.findings] == [r.id for r in layout.regions]
    empty = reader.read(tmp_path / "missing.html", layout=layout, region_ids=[])
    assert empty.outcome == "failed" and not empty.blocks
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(tmp_path / "missing.html", region_ids=[])


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        ArticleHtmlTextReader().read(BytesIO(PAGE.encode()))
