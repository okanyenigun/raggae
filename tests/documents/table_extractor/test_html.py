"""Tables declared in an HTML document, standalone or by layout region."""

import json
import sys
import xml.etree.ElementTree as ET

import pytest

from raggae.documents.layout_analysis import (
    HtmlDocumentLocation,
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.layout_analysis.region_discovery.html import HtmlLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    HtmlTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout


def codes(result):
    return [(f.code, f.region_id) for f in result.findings]


def grid(extracted):
    """Each cell as (row, column, row_span, column_span, text, header)."""
    return [
        (c.row, c.column, c.row_span, c.column_span, c.text, c.header)
        for c in extracted.cells
    ]


def extract(html, **options):
    data = html.encode() if isinstance(html, str) else html
    return HtmlTableExtractor().extract(data, **options)


def only(html):
    result = extract(html)
    assert len(result.tables) == 1, result
    return result.tables[0]


PAGE = """<!doctype html>
<html><head><title>Parts</title>
<script>document.write("<table><tr><td>never read</td></tr></table>")</script></head>
<body>
<h1>Price list</h1>
<table>
  <caption>Prices</caption>
  <thead>
    <tr><th rowspan="2">Part</th><th colspan="2">Price</th></tr>
    <tr><th>2025</th><th>2026</th></tr>
  </thead>
  <tbody>
    <tr><td>Motor</td><td>110</td><td>120</td></tr>
    <tr><td>Pump <img src="pump.png" alt="(photo)"></td><td>80</td><td>85</td></tr>
  </tbody>
</table>
<div><table><tr><td>Region</td><td>North</td></tr></table></div>
</body></html>"""

PRICES = [
    (0, 0, 2, 1, "Part", True),
    (0, 1, 1, 2, "Price", True),
    (1, 1, 1, 1, "2025", True),
    (1, 2, 1, 1, "2026", True),
    (2, 0, 1, 1, "Motor", False),
    (2, 1, 1, 1, "110", False),
    (2, 2, 1, 1, "120", False),
    (3, 0, 1, 1, "Pump (photo)", False),
    (3, 1, 1, 1, "80", False),
    (3, 2, 1, 1, "85", False),
]


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import html

    assert html.HtmlTableExtractor is HtmlTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = PAGE.encode()
    path = tmp_path / "prices.html"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = HtmlTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_html"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    prices, regions = result.tables
    assert grid(prices) == PRICES
    assert (prices.row_count, prices.column_count, prices.caption) == (4, 3, "Prices")
    assert (prices.method, prices.page_index, prices.region_id) == ("declared", 0, None)
    assert grid(regions) == [(0, 0, 1, 1, "Region", False), (0, 1, 1, 1, "North", False)]
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# The grid ---------------------------------------------------------------------------


def test_rowspan_stays_inside_its_row_group():
    extracted = only(
        "<table><thead><tr><th rowspan=0>A</th><th>B</th></tr><tr><th>C</th></tr></thead>"
        "<tbody><tr><td rowspan=9>x</td><td>y</td></tr><tr><td>z</td></tr></tbody></table>"
    )
    # rowspan=0 reaches the end of <thead>; rowspan=9 stops at the end of <tbody>.
    assert grid(extracted) == [
        (0, 0, 2, 1, "A", True),
        (0, 1, 1, 1, "B", True),
        (1, 1, 1, 1, "C", True),
        (2, 0, 2, 1, "x", False),
        (2, 1, 1, 1, "y", False),
        (3, 1, 1, 1, "z", False),
    ]


@pytest.mark.parametrize(
    ("colspan", "width"),
    [("0", 1), ("2px", 2), (" +3", 3), ("abc", 1), ("-2", 1), ("1001", 1000), ("9" * 5000, 1000)],
)
def test_span_values_are_read_as_html_reads_them(colspan, width):
    extracted = only(f'<table><tr><td colspan="{colspan}">a</td></tr></table>')
    assert extracted.cells[0].column_span == width == extracted.column_count


def test_a_span_running_into_another_cell_is_shortened():
    result = extract(
        "<table><tr><td>a</td><td rowspan=2>b</td></tr><tr><td colspan=2>c</td></tr></table>"
    )
    assert grid(result.tables[0])[2] == (1, 0, 1, 1, "c", False)
    assert result.outcome == "success" and codes(result) == [("span_shortened", None)]


def test_short_rows_are_not_padded_and_empty_rows_are_kept():
    extracted = only(
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr><tr></tr>"
        "<tr><td>d</td><td>e</td></tr></table>"
    )
    assert (extracted.row_count, extracted.column_count) == (4, 2)
    assert [(c.row, c.column) for c in extracted.cells] == [(0, 0), (0, 1), (1, 0), (3, 0), (3, 1)]


def test_rows_keep_source_order():
    extracted = only(
        "<table><tfoot><tr><td>total</td></tr></tfoot><tbody><tr><td>item</td></tr></tbody></table>"
    )
    assert [c.text for c in extracted.cells] == ["total", "item"]


# Cell text ---------------------------------------------------------------------------


def test_cell_text_is_what_the_html_text_reader_reads():
    result = extract(
        "<table><tr>"
        "<td>  Spaced \n   out  </td>"
        "<td>Line one<br>Line two</td>"
        "<td><p>First</p><p>Second</p></td>"
        "<td><b>Bold</b> and <img src='x.png' alt='an icon'></td>"
        "<td>Shown<span hidden> secret</span></td>"
        "<td>Press <button>OK</button></td>"
        "</tr></table>"
    )
    assert [c.text for c in result.tables[0].cells] == [
        "Spaced out",
        "Line one\nLine two",
        "First\nSecond",
        "Bold and an icon",
        "Shown",
        "Press",
    ]
    assert codes(result) == [("hidden_skipped", None), ("unsupported_content", None)]
    assert result.outcome == "success"


def test_a_nested_table_is_read_into_its_cell():
    result = extract(
        "<table><tr><th>Item</th><th>Parts</th></tr>"
        "<tr><td>Kit</td><td><table><tr><td>bolt</td><td>2</td></tr>"
        "<tr><td>nut</td><td>4</td></tr></table></td></tr></table>"
    )
    assert len(result.tables) == 1
    assert result.tables[0].cells[-1].text == "bolt | 2\nnut | 4"
    assert codes(result) == [("nested_table", None)]
    hidden = extract(
        "<table><tr><td>Kit<table hidden><tr><td>bolt</td></tr></table></td></tr></table>"
    )
    assert hidden.tables[0].cells[0].text == "Kit"
    assert codes(hidden) == [("hidden_skipped", None)]


def test_tables_without_text_are_not_returned():
    result = extract(
        "<table><caption>Empty</caption><tr><td> </td><td></td></tr></table>"
        "<table></table><table><tr><td>x</td></tr></table>"
    )
    assert [grid(t) for t in result.tables] == [[(0, 0, 1, 1, "x", False)]]
    assert result.outcome == "success"
    assert codes(result) == [("empty_table", None), ("empty_table", None)]


def test_never_read_hidden_and_unsupported_content():
    result = extract(
        "<template><table><tr><td>t</td></tr></table></template>"
        "<noscript><table><tr><td>n</td></tr></table></noscript>"
        "<div hidden><table><tr><td>h</td></tr></table></div>"
        "<object><table><tr><td>o</td></tr></table></object>"
        "<table><caption hidden>Secret</caption>"
        "<tr><td>a</td><td hidden>skip</td><td>b</td></tr>"
        "<tr hidden><td>gone</td></tr><tr><td>c</td></tr></table>"
    )
    (extracted,) = result.tables
    # A hidden cell takes no position, as a browser lays it out; a hidden row is gone.
    assert grid(extracted) == [
        (0, 0, 1, 1, "a", False),
        (0, 1, 1, 1, "b", False),
        (1, 0, 1, 1, "c", False),
    ]
    assert extracted.caption is None
    assert codes(result) == [("hidden_skipped", None), ("unsupported_content", None)]


def test_documents_without_tables_including_empty_ones():
    for html in ("<p>Words only.</p>", ""):
        result = extract(html)
        assert result.outcome == "success" and codes(result) == [("empty", None)]
        assert result.findings[0].page_index == 0


def test_encoding_is_configured_never_guessed():
    data = '<meta charset="cp1252"><table><tr><td>Café</td></tr></table>'.encode("cp1252")
    assert HtmlTableExtractor(encoding="cp1252").extract(data).tables[0].cells[0].text == "Café"
    failed = HtmlTableExtractor().extract(data)
    assert failed.outcome == "failed" and codes(failed) == [("decode_failed", None)]


@pytest.mark.parametrize("encoding", [None, 1, "latin-9"])
def test_encoding_option_is_validated(encoding):
    with pytest.raises((TypeError, ValueError)):
        HtmlTableExtractor(encoding=encoding)


def test_missing_file_and_missing_beautifulsoup(tmp_path, monkeypatch):
    assert codes(extract(tmp_path / "missing.html")) == [("source_unreadable", None)]
    monkeypatch.setitem(sys.modules, "bs4", None)
    with pytest.raises(ImportError, match=r"raggae\[text-html\]"):
        extract(PAGE)


def test_deep_nesting_is_reported_not_raised():
    html = "<table><tr><td>" + "<span>" * 5000 + "deep" + "</span>" * 5000 + "</td></tr></table>"
    result = extract(html)
    assert result.outcome == "failed"
    assert codes(result) == [("structure_unsupported", None)]


# The forms of extracted tables ----------------------------------------------------------

FIXTURES = [
    PAGE,
    "<table><tr><th rowspan=0>A</th><th>B</th></tr><tr><th>C</th></tr>"
    "<tr><td colspan=2>wide</td></tr></table>",
    "<table><caption>Two\nlines<br>here</caption><tr><td>a &amp; b<br>c</td><td>x</td></tr>"
    "<tr><td>short</td></tr><tr></tr></table>",
]


@pytest.mark.parametrize("html", FIXTURES)
def test_to_html_reads_back_into_the_same_table(html):
    for extracted in extract(html).tables:
        (again,) = extract(extracted.to_html()).tables
        assert again == extracted


@pytest.mark.parametrize("html", FIXTURES)
def test_the_row_based_forms_agree(html):
    for extracted in extract(html).tables:
        records = json.loads(extracted.to_json())
        assert extracted.to_dataframe().to_dict("records") == records
        root = ET.fromstring(extracted.to_xml())
        from_xml = [
            {value.get("column"): value.text or "" for value in row}
            for row in root.findall("row")
        ]
        assert from_xml == [
            {key: value for key, value in record.items() if value is not None}
            for record in records
        ]
        lines = extracted.to_markdown().splitlines()
        assert len(lines) == 2 + len(records)


# Layout regions -----------------------------------------------------------------------


@pytest.fixture
def layout():
    return HtmlLayoutAnalyzer().analyze(PAGE.encode())


def ids(layout, tag):
    return [r.id for r in layout.regions if r.location.tag == tag]


def test_layout_links_each_table_to_its_table_region(layout):
    tables = ids(layout, "table")
    result = extract(PAGE, layout=layout)
    assert [t.region_id for t in result.tables] == tables
    assert grid(result.tables[0]) == PRICES
    assert result.outcome == "success" and not result.findings


def test_explicit_regions_of_any_kind(layout):
    (heading,) = ids(layout, "h1")
    (image,) = ids(layout, "img")
    first = ids(layout, "table")[0]
    result = extract(PAGE, layout=layout, region_ids=[heading, first, image])
    assert [t.region_id for t in result.tables] == [first]
    # The picture belongs to its own selected region, so its alt text leaves the cell.
    assert result.tables[0].cells[7].text == "Pump"
    assert codes(result) == [("empty", heading), ("empty", image)]


def custom(*regions):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="document", kind="flow", location=HtmlDocumentLocation(element="document")
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=identifier,
                surface_id="document",
                kind=kind,
                location=HtmlElementLocation(child_indexes=path, tag=tag),
            )
            for identifier, kind, path, tag in regions
        ),
    )


HTML = (
    "<html><head><title>t</title></head><body>"
    "<div><table><tr><td>inside</td></tr></table></div>"
    "<div hidden><table><tr><td>hidden</td></tr></table></div>"
    "<svg></svg>"
    "<table><tr><td>last</td></tr></table>"
    "</body></html>"
)


def test_the_innermost_region_owns_a_table():
    layout = custom(
        ("box", "unknown", (0, 1, 0), "div"),
        ("table", "table", (0, 1, 0, 0), "table"),
    )
    alone = extract(HTML, layout=layout, region_ids=["box"])
    assert [(t.region_id, t.cells[0].text) for t in alone.tables] == [("box", "inside")]
    both = extract(HTML, layout=layout, region_ids=["box", "table"])
    assert [(t.region_id, t.cells[0].text) for t in both.tables] == [("table", "inside")]
    assert codes(both) == [("empty", "box")]


def test_unresolved_unsupported_hidden_and_overlapping_regions():
    layout = custom(
        ("hidden", "table", (0, 1, 1, 0), "table"),
        ("vector", "unknown", (0, 1, 2), "svg"),
        ("title", "text", (0, 0, 0), "title"),
        ("wrong-tag", "table", (0, 1, 3), "p"),
        ("far", "table", (0, 1, 9), "table"),
        ("twin-a", "table", (0, 1, 3), "table"),
        ("twin-b", "table", (0, 1, 3), "table"),
    )
    # Asked for by ID: without region_ids only the table regions would be used.
    result = extract(HTML, layout=layout, region_ids=[r.id for r in layout.regions])
    assert result.outcome == "partial" and not result.tables
    assert codes(result) == [
        ("selection_unsupported", "vector"),
        ("selection_unsupported", "title"),
        ("region_unresolved", "wrong-tag"),
        ("region_unresolved", "far"),
        ("hidden_skipped", "hidden"),
        ("selection_overlap", "twin-a"),
        ("selection_overlap", "twin-b"),
    ]


def test_non_html_locations_and_empty_selections(tmp_path):
    pdf_layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = extract(PAGE, layout=pdf_layout)
    assert result.outcome == "failed" and codes(result) == [("selection_unsupported", "grid")]
    missing = tmp_path / "missing.html"
    extractor = HtmlTableExtractor()
    assert codes(extractor.extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
    no_tables = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    result = extractor.extract(missing, layout=no_tables)
    assert result.outcome == "success" and codes(result) == [("nothing_selected", None)]
    assert "no table regions" in result.findings[0].message

