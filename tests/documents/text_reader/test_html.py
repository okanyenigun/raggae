"""Stored HTML text by element, standalone or from HTML layout regions."""

import builtins
import socket
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.layout_analysis.models import (
    HtmlDocumentLocation,
    HtmlElementLocation,
)
from raggae.documents.layout_analysis.region_discovery.html import HtmlLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import HtmlTextReader, TextReader, TextReadResult


def texts(result):
    return [block.text for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def read(markup, **kwargs):
    if isinstance(markup, str):
        markup = markup.encode()
    return HtmlTextReader().read(markup, **kwargs)


def html_layout(*paths):
    """A hand-built map; each item is (child_indexes, tag) or (child_indexes, tag, kind)."""
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="html",
                kind="flow",
                location=HtmlDocumentLocation(element="document"),
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=f"r{number}",
                surface_id="html",
                kind=item[2] if len(item) > 2 else "text",
                location=HtmlElementLocation(child_indexes=item[0], tag=item[1]),
                native_order=number - 1,
            )
            for number, item in enumerate(paths, start=1)
        ),
    )


def by_region(result):
    return {block.region_id: block.text for block in result.blocks}


# Standalone reading -------------------------------------------------------


def test_html_family_exports_the_same_worker():
    from raggae.documents.text_reader import html as html_readers

    assert html_readers.HtmlTextReader is HtmlTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = "<h1>Garantie</h1><p>Café İstanbul</p>".encode()
    path = tmp_path / "page.html"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = HtmlTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_html"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["Garantie", "Café İstanbul"]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_one_block_per_text_element():
    result = read(
        "<h1>Warranty</h1><p>Covers two years.</p><ul><li>Parts</li><li>Labour</li></ul>"
        "<dl><dt>Term</dt><dd>Two years</dd></dl><figure><figcaption>Motor</figcaption></figure>"
    )
    assert texts(result) == [
        "Warranty",
        "Covers two years.",
        "Parts",
        "Labour",
        "Term",
        "Two years",
        "Motor",
    ]


def test_whitespace_collapses_like_a_browser_and_inline_markup_joins():
    result = read(
        "<p>  Covers <b>two</b>\n\t years,<i> parts</i>&amp;<a href='x'>labour</a> </p>"
    )
    assert texts(result) == ["Covers two years, parts&labour"]


def test_entities_and_non_breaking_spaces_are_decoded_not_collapsed():
    result = read("<p>a&nbsp;&nbsp;b &lt;c&gt; &#x1F600;</p>")
    assert texts(result) == ["a\xa0\xa0b <c> \U0001f600"]


def test_line_breaks_become_newlines():
    result = read("<p>One<br>Two <br/> Three<br><br>Five</p>")
    assert texts(result) == ["One\nTwo\nThree\n\nFive"]


def test_nested_blocks_inside_a_text_element_stay_in_that_block():
    result = read(
        "<ul><li>Parts<ul><li>Motor</li><li>Gears</li></ul></li><li>Labour</li></ul>"
        "<blockquote><p>First</p><p>Second</p></blockquote>"
    )
    assert texts(result) == ["Parts\nMotor\nGears", "Labour", "First\nSecond"]


def test_loose_container_text_forms_its_own_blocks():
    result = read("<div>Intro<p>Body</p>Thanks <span>again</span></div>")
    assert texts(result) == ["Intro", "Body", "Thanks again"]


def test_text_outside_any_element_is_read():
    result = read("Loose text<p>Para</p>")
    assert texts(result) == ["Loose text", "Para"]


def test_pre_keeps_whitespace_and_drops_one_leading_newline():
    result = read("<pre>\n  indented\tcode\n\nnext  </pre><pre>\r\nwin</pre>")
    assert texts(result) == ["  indented\tcode\n\nnext  ", "win"]


def test_pre_inside_a_block_keeps_its_own_lines():
    result = read("<blockquote>Quote:<pre>  a\n  b</pre>end</blockquote>")
    assert texts(result) == ["Quote:\n  a\n  b\nend"]


def test_tables_put_rows_on_lines_and_cells_joined_by_bars():
    result = read(
        "<table><caption>Cover</caption>"
        "<thead><tr><th>Part</th><th>Years</th></tr></thead>"
        "<tbody><tr><td>Motor</td><td>2<br>max</td></tr>"
        "<tr><td></td><td>  </td></tr>"
        "<tr><td>Gears</td><td></td></tr></tbody></table>"
    )
    assert texts(result) == ["Cover\nPart | Years\nMotor | 2 max\nGears | "]


def test_nested_table_text_stays_in_its_cell_without_duplicate_rows():
    result = read(
        "<table><tr><td>Outer</td><td><table><tr><td>In1</td><td>In2</td></tr>"
        "</table></td></tr></table>"
    )
    assert texts(result) == ["Outer | In1 | In2"]


def test_table_inside_a_list_item_keeps_its_rows():
    result = read("<li>Prices<table><tr><td>a</td><td>1</td></tr></table></li>")
    assert texts(result) == ["Prices\na | 1"]


def test_image_alt_text_is_read_inline_and_as_its_own_block():
    result = read(
        "<p>See <img src='m.png' alt='motor  diagram'> below.</p>"
        "<img alt='Front view'><img src='no-alt.png'><p>x<img alt=''>y</p>"
    )
    assert texts(result) == ["See motor diagram below.", "Front view", "xy"]


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert('x')</script>",
        "<style>p { color: red }</style>",
        "<!-- private comment -->",
        "<template><p>inert</p></template>",
        "<noscript><p>enable js</p></noscript>",
        "<head><title>Page title</title><meta charset='utf-8'></head>",
        "<title>Bare title</title>",
        "<![CDATA[cdata]]>",
        "<!DOCTYPE html>",
        "<?xml version='1.0'?>",
    ],
)
def test_non_text_content_is_never_read(markup):
    result = read(markup + "<p>Body</p>")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["Body"]


def test_hidden_elements_are_skipped_with_one_finding():
    result = read(
        "<p>Visible <span hidden>secret</span>text</p><p hidden>Internal</p>"
        "<div hidden><p>Also</p></div><table><tr hidden><td>row</td></tr>"
        "<tr><td>shown</td></tr></table>"
    )
    assert result.outcome == "success"
    assert texts(result) == ["Visible text", "shown"]
    assert codes(result) == ["hidden_skipped"]
    assert result.findings[0].page_index == 0
    assert "secret" not in result.model_dump_json()


def test_css_hidden_content_is_still_read_because_css_is_not_applied():
    result = read("<p style='display:none'>Styled away</p>")
    assert texts(result) == ["Styled away"]


@pytest.mark.parametrize(
    "markup",
    [
        "<button>Buy</button>",
        "<textarea>draft</textarea>",
        "<select><option>One</option></select>",
        "<svg><text>vector</text></svg>",
        "<iframe>fallback</iframe>",
        "<video>no video</video>",
    ],
)
def test_unsupported_elements_are_skipped_with_a_finding(markup):
    result = read(f"<p>Before {markup} after</p>")
    assert result.outcome == "success"
    assert texts(result) == ["Before after"]
    assert codes(result) == ["unsupported_content"]


@pytest.mark.parametrize(
    "markup", ["", "   \n", "<html><body></body></html>", "<p> </p><img alt=''>"]
)
def test_no_readable_text_succeeds_with_an_explanation(markup):
    result = read(markup)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


def test_repeated_wording_is_not_deduplicated():
    assert texts(read("<p>Same</p><p>Same</p>")) == ["Same", "Same"]


def test_does_not_fetch_or_import_article_extractors(monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"trafilatura", "requests", "urllib3"}:
            raise AssertionError(f"unexpected import of {name}")
        return real_import(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    result = read(
        "<link rel='stylesheet' href='http://x/s.css'><img src='http://x/i.png' alt='i'>"
        "<iframe src='http://x'></iframe><script src='http://x/a.js'></script>"
    )
    assert texts(result) == ["i"]


def test_missing_backend_has_an_install_hint(monkeypatch):
    real_import = builtins.__import__

    def without_bs4(name, *args, **kwargs):
        if name == "bs4" or name.startswith("bs4."):
            raise ImportError("no bs4")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_bs4)
    with pytest.raises(ImportError, match=r"raggae\[text-html\]"):
        read("<p>x</p>")


def test_too_deep_nesting_is_reported_without_changing_recursion_limit():
    import sys

    limit = sys.getrecursionlimit()
    result = read("<div>" * 5000 + "x" + "</div>" * 5000)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["structure_unsupported"]
    assert sys.getrecursionlimit() == limit


# Encoding and source handling ----------------------------------------------


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"],
)
def test_declared_encodings(encoding):
    word = "Cafe" if encoding == "ascii" else "Café"
    result = HtmlTextReader(encoding=encoding).read(f"<p>{word}</p>".encode(encoding))
    assert texts(result) == [word]


def test_meta_charset_is_not_used_to_guess_the_encoding():
    source = "<meta charset='iso-8859-1'><p>Café</p>".encode("iso-8859-1")
    result = read(source)
    assert result.outcome == "failed"
    assert codes(result) == ["decode_failed"]


def test_default_utf8_keeps_a_bom_as_text_and_utf8_sig_removes_it():
    assert texts(read(b"\xef\xbb\xbf<p>x</p>")) == ["﻿", "x"]
    sig = HtmlTextReader(encoding="utf-8-sig").read(b"\xef\xbb\xbf<p>x</p>")
    assert texts(sig) == ["x"]


def test_decode_failure_is_private():
    result = read(b"<p>private\xff</p>")
    assert result.outcome == "failed" and not result.blocks
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = HtmlTextReader()
    result = reader.read(tmp_path / "private-name.html")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    assert reader.read(tmp_path).outcome == "failed"
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


@pytest.mark.parametrize(
    "kwargs,error",
    [({"encoding": None}, TypeError), ({"encoding": "unknown-codec"}, ValueError)],
)
def test_configuration_errors(kwargs, error):
    with pytest.raises(error):
        HtmlTextReader(**kwargs)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        HtmlTextReader().read(BytesIO(b"<p>x</p>"))


# Layout-guided reading -----------------------------------------------------

PAGE = (
    "<html><head><title>Shop</title></head><body>"
    "<h1>Warranty</h1>"
    "<div>Intro<p>See <img alt='motor diagram'> below.</p>Thanks</div>"
    "<table><tr><td>Motor</td><td>2</td></tr></table>"
    "<p hidden>Internal</p>"
    "<button>Buy</button>"
    "</body></html>"
)


@pytest.fixture
def page_layout():
    return HtmlLayoutAnalyzer().analyze(PAGE.encode())


def region_by_tag(layout, tag):
    return next(r for r in layout.regions if r.location.tag == tag)


def test_layout_regions_from_the_html_analyzer_are_read(page_layout):
    h1 = region_by_tag(page_layout, "h1")
    table = region_by_tag(page_layout, "table")
    result = read(PAGE, layout=page_layout, region_ids=[table.id, h1.id])
    assert result.outcome == "success" and not result.findings
    assert [(b.region_id, b.text) for b in result.blocks] == [
        (table.id, "Motor | 2"),
        (h1.id, "Warranty"),
    ]
    assert all(
        b.page_index == 0 and b.content_format == "plain_text" for b in result.blocks
    )


def test_inner_selected_region_owns_its_text(page_layout):
    div = region_by_tag(page_layout, "div")
    p = next(
        r for r in page_layout.regions if r.location.tag == "p" and r.kind == "text"
    )
    img = region_by_tag(page_layout, "img")
    both = read(PAGE, layout=page_layout, region_ids=[div.id, p.id, img.id])
    assert by_region(both) == {
        div.id: "Intro\nThanks",
        p.id: "See below.",
        img.id: "motor diagram",
    }
    outer_only = read(PAGE, layout=page_layout, region_ids=[div.id])
    assert by_region(outer_only) == {div.id: "Intro\nSee motor diagram below.\nThanks"}


def test_selecting_all_regions_returns_each_text_once(page_layout):
    result = read(PAGE, layout=page_layout)
    assert result.outcome == "partial"  # The button region cannot be read.
    joined = "\n".join(texts(result))
    for piece in (
        "Warranty",
        "Intro",
        "Thanks",
        "See below.",
        "motor diagram",
        "Motor | 2",
    ):
        assert joined.count(piece) == 1
    assert "Shop" not in joined and "Internal" not in joined and "Buy" not in joined
    button = region_by_tag(page_layout, "button")
    hidden = next(
        r
        for r in page_layout.regions
        if r.location.tag == "p" and r.id not in by_region(result)
    )
    assert ("selection_unsupported", button.id) in [
        (f.code, f.region_id) for f in result.findings
    ]
    assert ("hidden_skipped", hidden.id) in [
        (f.code, f.region_id) for f in result.findings
    ]


def test_hidden_region_is_skipped_but_counts_as_read(page_layout):
    hidden = next(
        r
        for r in page_layout.regions
        if r.location.tag == "p" and r.location.child_indexes[-1] == 3
    )
    result = read(PAGE, layout=page_layout, region_ids=[hidden.id])
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("hidden_skipped", hidden.id)
    ]


def test_unsupported_region_is_rejected(page_layout):
    button = region_by_tag(page_layout, "button")
    result = read(PAGE, layout=page_layout, region_ids=[button.id])
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["selection_unsupported"]
    assert result.findings[0].region_id == button.id


def test_image_region_without_alt_is_an_empty_read():
    layout = html_layout(((0,), "img", "image"))
    result = read("<img src='x.png'>", layout=layout)
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [("empty", "r1")]


def test_region_text_excludes_scripts_and_notes_unsupported_children():
    layout = html_layout(((0,), "div"))
    result = read(
        "<div>Keep<script>x()</script><button>Buy</button> going</div>", layout=layout
    )
    assert result.outcome == "success"
    assert by_region(result) == {"r1": "Keep going"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("unsupported_content", "r1")
    ]


@pytest.mark.parametrize(
    "path,tag",
    [((5,), "p"), ((0, 0), "p"), ((0,), "div"), ((0, 0, 0, 0), "b")],
)
def test_unresolved_or_mismatched_locations_are_reported(path, tag):
    layout = html_layout(((0,), "p"), (path, tag))
    result = read("<p>Kept</p>", layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r1": "Kept"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("region_unresolved", "r2")
    ]


def test_element_indexes_ignore_text_comments_and_a_bom():
    layout = html_layout(((1,), "p"))
    result = HtmlTextReader(encoding="utf-8").read(
        b"\xef\xbb\xbftext<!-- c --><p>first</p>more<p>second</p>", layout=layout
    )
    assert by_region(result) == {"r1": "second"}


def test_regions_inside_never_read_content_are_rejected():
    layout = html_layout(((0, 0), "title"), ((1, 0), "p"))
    result = read(
        "<head><title>T</title></head><template><p>x</p></template>", layout=layout
    )
    assert result.outcome == "failed"
    assert codes(result) == ["selection_unsupported"] * 2


def test_two_regions_locating_the_same_element_are_both_rejected():
    layout = html_layout(((0,), "p"), ((0,), "p"), ((1,), "p"))
    result = read("<p>Same</p><p>Other</p>", layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r3": "Other"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_overlap", "r1"),
        ("selection_overlap", "r2"),
    ]


def test_non_html_locations_are_rejected_without_reading_another_area():
    layout = LayoutAnalysisResult(
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
                id="pdf-r1", surface_id="page-1", kind="text", bbox=(0, 0, 20, 20)
            ),
        ),
    )
    result = read("<p>Whole page</p>", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "pdf-r1")
    ]


def test_empty_selection_reads_nothing_and_says_so(tmp_path, monkeypatch, page_layout):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = HtmlTextReader().read(
        tmp_path / "missing.html", layout=page_layout, region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_layout_argument_validation(tmp_path, page_layout):
    reader = HtmlTextReader()
    source = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=page_layout, region_ids=["unknown"])
    with pytest.raises(ValueError, match="duplicates"):
        reader.read(source, layout=page_layout, region_ids=["html-r1", "html-r1"])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")
    with pytest.raises(TypeError, match="sequence"):
        reader.read(source, layout=page_layout, region_ids="html-r1")
    failed = LayoutAnalysisResult(outcome="failed", findings=page_layout.findings or ())
    with pytest.raises(ValueError, match="usable map"):
        reader.read(source, layout=failed)


def test_source_read_failure_in_layout_mode_is_document_wide(tmp_path, page_layout):
    result = HtmlTextReader().read(tmp_path / "missing.html", layout=page_layout)
    assert result.outcome == "failed"
    assert codes(result) == ["source_unreadable"]
