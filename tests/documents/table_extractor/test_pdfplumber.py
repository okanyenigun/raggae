"""Tables pdfplumber finds on PDF pages, standalone or by layout region."""

import sys
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    HtmlDocumentLocation,
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    NativePdfLayoutAnalyzer,
)
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    PdfplumberTableExtractor,
    TableCell,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout
from tests.documents.table_extractor.conftest import (
    EMPTY_GRID,
    RULED,
    RULED_GRID,
    TWO_LINES,
    UNRULED,
    make_pdf,
    ruled,
    text,
)


@pytest.fixture(autouse=True)
def backend():
    return pytest.importorskip("pdfplumber", reason="Install raggae[text-pdf]")


def codes(result):
    return [(f.code, f.region_id) for f in result.findings]


def grid(table):
    return [(c.row, c.column, c.row_span, c.column_span, c.text) for c in table.cells]


def extract(data, **options):
    strategy = options.pop("strategy", "lines")
    return PdfplumberTableExtractor(strategy=strategy).extract(data, **options)


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import pdf

    assert pdf.PdfplumberTableExtractor is PdfplumberTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = make_pdf(RULED)
    path = tmp_path / "prices.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(path=path),
    }[source_kind]
    extractor = PdfplumberTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_pdfplumber"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    # Spans are measured from the cell boxes; the heading is not part of the table.
    assert grid(table) == RULED_GRID
    assert (table.row_count, table.column_count) == (3, 3)
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "ruled",
        0,
        None,
        None,
    )
    assert not any(cell.header for cell in table.cells)
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_tables_keep_their_page():
    data = make_pdf(RULED, text(30, 330, "No table here"), RULED)
    result = extract(data)
    assert [t.page_index for t in result.tables] == [0, 2]
    assert result.outcome == "success" and not result.findings


def test_the_lines_strategy_does_not_see_tables_without_borders():
    result = extract(make_pdf(UNRULED))
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty", None)]
    assert result.findings[0].page_index is None


def test_the_text_strategy_infers_tables_from_alignment():
    (table,) = extract(make_pdf(UNRULED), strategy="text").tables
    assert table.method == "aligned"
    rows = {}
    for cell in table.cells:
        rows.setdefault(cell.row, []).append(cell.text)
    # pdfplumber makes each gap between text lines a row; those rows have no
    # text and are dropped. The footnote under the table is a row, cut at the
    # cell edges.
    assert table.row_count == len(rows) == 6
    assert rows[0] == ["Region", "Year", "Units"]
    assert rows[1] == ["North", "2025", "1200"]
    assert "Figures are round" in rows[5][0]


def test_a_ruled_grid_keeps_its_empty_rows():
    drawn = (
        b"0.5 w 30 200 240 60 re S 30 220 m 270 220 l S 30 240 m 270 240 l S "
        b"150 200 m 150 260 l S\n"
        + text(35, 245, "Top")
        + text(35, 205, "Bottom")
    )
    (table,) = extract(make_pdf(drawn)).tables
    assert table.row_count == 3
    assert [(c.row, c.text) for c in table.cells] == [
        (0, "Top"),
        (0, ""),
        (1, ""),
        (1, ""),
        (2, "Bottom"),
        (2, ""),
    ]


def test_dropping_rows_without_text_keeps_spans_consistent():
    cells = [
        TableCell(row=0, column=0, row_span=3, text="Region"),
        TableCell(row=0, column=1, text="a"),
        TableCell(row=1, column=1, text=""),
        TableCell(row=2, column=1, text="b"),
        TableCell(row=3, column=0, text=""),
        TableCell(row=3, column=1, text=""),
    ]
    kept, row_count = PdfplumberTableExtractor._text_rows(cells, 4)
    assert row_count == 2
    assert [(c.row, c.column, c.row_span, c.text) for c in kept] == [
        (0, 0, 2, "Region"),
        (0, 1, 1, "a"),
        (1, 1, 1, "b"),
    ]


def test_a_rectangle_keeps_the_text_strategy_to_the_table():
    layout = make_layout(("units", 1, "table", (30, 45, 280, 140)))
    result = extract(make_pdf(UNRULED), strategy="text", layout=layout)
    (table,) = result.tables
    texts = [cell.text for cell in table.cells]
    assert table.region_id == "units" and texts[:3] == ["Region", "Year", "Units"]
    assert "South" in texts and not any("Figures" in t for t in texts)


@pytest.mark.parametrize(
    ("strategy", "error"), [("explicit", ValueError), ("", ValueError), (1, TypeError)]
)
def test_strategy_is_validated(strategy, error):
    with pytest.raises(error):
        PdfplumberTableExtractor(strategy=strategy)


# Units sold by region, aligned without lines, above the ruled price table.
BOTH = b"".join(
    text(x, 340 - number * 16, word)
    for number, row in enumerate(
        [
            ("Region", "Year", "Units"),
            ("North", "2025", "1200"),
            ("North", "2026", "1350"),
            ("South", "2025", "900"),
        ]
    )
    for x, word in zip((40, 120, 200), row)
) + ruled(dy=-200)


def test_all_finds_ruled_and_aligned_tables_top_to_bottom():
    result = extract(make_pdf(BOTH), strategy="all")
    assert result.outcome == "success" and not result.findings
    units, prices = result.tables
    assert (units.method, prices.method) == ("aligned", "ruled")
    assert [c.text for c in units.cells[:3]] == ["Region", "Year", "Units"]
    assert units.row_count == 4 and grid(prices) == RULED_GRID


def test_without_a_layout_the_default_looks_for_lines_only():
    # On a whole page the text strategy would also turn paragraphs into tables.
    (table,) = PdfplumberTableExtractor().extract(make_pdf(BOTH)).tables
    assert table.method == "ruled" and grid(table) == RULED_GRID


def test_all_keeps_ruled_text_out_of_aligned_tables():
    def holds_ruled_text(tables):
        # Fragments, since the text strategy cuts words at its cell edges.
        texts = [c.text for t in tables if t.method == "aligned" for c in t.cells]
        return any(part in t for t in texts for part in ("otor", "rice", "art"))

    # The text strategy alone reads the ruled table's words as aligned text too.
    assert holds_ruled_text(extract(make_pdf(BOTH), strategy="text").tables)
    assert not holds_ruled_text(extract(make_pdf(BOTH), strategy="all").tables)


def test_with_a_layout_the_default_reads_each_region_the_way_it_is_drawn():
    layout = make_layout(
        ("units", 1, "table", (30, 45, 280, 130)),
        ("prices", 1, "table", (20, 240, 280, 330)),
    )
    result = PdfplumberTableExtractor().extract(make_pdf(BOTH), layout=layout)
    assert result.outcome == "success" and not result.findings
    assert [(t.region_id, t.method) for t in result.tables] == [
        ("units", "aligned"),
        ("prices", "ruled"),
    ]
    assert grid(result.tables[1]) == RULED_GRID


def test_grids_without_text_are_not_returned():
    result = extract(make_pdf(EMPTY_GRID))
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty_table", None)]
    assert result.findings[0].page_index == 0


def test_cell_text_keeps_line_breaks():
    (table,) = extract(make_pdf(TWO_LINES)).tables
    assert [c.text for c in table.cells] == ["First line\nSecond line", "Other"]


def test_only_the_visible_area_is_read():
    lower = b"0.5 w 30 60 240 40 re S 150 60 m 150 100 l S\n" + text(35, 75, "Hidden")
    data = make_pdf(RULED + lower, crop=(0, 150, 300, 400))
    (table,) = extract(data).tables
    assert grid(table) == RULED_GRID


@pytest.mark.parametrize(
    ("rotation", "origin", "unit"),
    [(0, (0, 0), 1), (90, (0, 0), 1), (180, (-40, 25), 1), (270, (15, -30), 2)],
)
def test_coordinates_survive_crop_rotation_origin_and_userunit(rotation, origin, unit):
    dx, dy = origin
    data = make_pdf(
        ruled(dx, dy),
        rotation=rotation,
        unit=unit,
        media=(dx, dy, 300 + dx, 400 + dy),
        crop=(20 + dx, 30 + dy, 290 + dx, 395 + dy),
    )
    layout = NativePdfLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    (standalone,) = extract(data).tables
    (by_region,) = extract(data, layout=layout).tables
    # The grid is as displayed: on a quarter-turned page, rows and columns turn too.
    assert by_region.region_id == region.id
    assert grid(by_region) == grid(standalone)
    assert sorted(c.text for c in standalone.cells) == sorted(t for *_, t in RULED_GRID)
    if rotation == 0:
        assert grid(standalone) == RULED_GRID


@pytest.mark.parametrize(
    ("password", "code"),
    [(None, "password_required"), ("private-wrong", "invalid_password")],
)
def test_password_errors_are_safe(password, code):
    data = make_pdf(RULED, password="private-secret")
    result = extract(Artifact(data=data, password=password))
    assert result.outcome == "failed" and codes(result) == [(code, None)]
    assert "private" not in result.model_dump_json()


def test_the_password_comes_from_the_artifact():
    data = make_pdf(RULED, password="table-demo-secret")
    result = extract(Artifact(data=data, password="table-demo-secret"))
    assert grid(result.tables[0]) == RULED_GRID
    assert "table-demo-secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("data", "outcome", "code"),
    [
        (b"plain text, not a PDF", "not_applicable", "not_applicable"),
        (b"", "failed", "decode_failed"),
        (b"%PDF-1.7\nbroken", "failed", "decode_failed"),
    ],
)
def test_bad_sources(data, outcome, code):
    result = extract(data)
    assert result.outcome == outcome and codes(result) == [(code, None)]


def test_missing_and_irregular_files(tmp_path):
    assert codes(extract(tmp_path / "missing.pdf")) == [("source_unreadable", None)]
    assert codes(extract(tmp_path)) == [("source_unreadable", None)]


def test_a_page_failure_keeps_other_pages_and_hides_parser_errors(monkeypatch):
    from pdfplumber.page import Page
    from pdfplumber.utils.exceptions import PdfminerException

    original = Page.parse_objects

    def broken(self):
        if self.page_number == 1:
            raise PdfminerException("private document details")
        return original(self)

    monkeypatch.setattr(Page, "parse_objects", broken)
    result = extract(make_pdf(RULED, RULED))
    assert result.outcome == "partial"
    assert [t.page_index for t in result.tables] == [1]
    assert codes(result) == [("page_unreadable", None)]
    assert result.findings[0].page_index == 0
    assert "private" not in result.model_dump_json()


def test_missing_pdfplumber_raises_an_install_instruction(monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    with pytest.raises(ImportError, match=r"raggae\[text-pdf\]"):
        extract(make_pdf(RULED))


def test_a_span_stops_where_another_cell_starts():
    # A box reaching over the next column, which another cell starts in.
    rows = [
        SimpleNamespace(cells=[(0, 0, 40, 20), (20, 0, 40, 10)]),
        SimpleNamespace(cells=[None, (20, 10, 40, 20)]),
    ]
    cells, row_count, column_count, shortened = PdfplumberTableExtractor._grid(
        rows, [], 0
    )
    assert [(c.row, c.column, c.row_span, c.column_span) for c in cells] == [
        (0, 0, 2, 1),
        (0, 1, 1, 1),
        (1, 1, 1, 1),
    ]
    assert (row_count, column_count, shortened) == (2, 2, True)


# Layout regions -------------------------------------------------------------------------


def test_layout_links_each_table_to_its_table_region():
    data = make_pdf(RULED)
    layout = NativePdfLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    result = extract(data, layout=layout)
    assert [t.region_id for t in result.tables] == [region.id]
    assert grid(result.tables[0]) == RULED_GRID
    assert result.outcome == "success" and not result.findings


def test_explicit_regions_of_any_kind_are_searched():
    data = make_pdf(RULED)
    layout = NativePdfLayoutAnalyzer().analyze(data)
    (heading,) = [r.id for r in layout.regions if r.kind == "text"]
    result = extract(data, layout=layout, region_ids=[heading])
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty", heading)]
    assert result.findings[0].page_index == 0


def test_regions_sharing_characters_are_both_refused():
    layout = make_layout(
        ("whole", 1, "table", (20, 40, 280, 130)),
        ("same", 1, "table", (25, 45, 275, 128)),
        ("heading", 1, "text", (25, 5, 150, 30)),
    )
    result = extract(make_pdf(RULED), layout=layout, region_ids=["whole", "same", "heading"])
    assert result.outcome == "partial" and not result.tables
    assert codes(result) == [
        ("selection_overlap", "whole"),
        ("selection_overlap", "same"),
        ("empty", "heading"),
    ]


def test_missing_pages_wrong_sizes_and_other_surfaces():
    data = make_pdf(RULED)
    far = make_layout(("far", 2, "table", (20, 40, 280, 130)), pages=2)
    result = extract(data, layout=far)
    assert result.outcome == "failed" and codes(result) == [("page_missing", "far")]
    assert result.findings[0].page_index == 1
    larger = make_layout(("big", 1, "table", (20, 40, 280, 130)), size=(600, 800))
    assert codes(extract(data, layout=larger)) == [("layout_mismatch", "big")]
    html = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="html", kind="flow", location=HtmlDocumentLocation(element="document")
            ),
        ),
        regions=(
            LayoutRegion(
                id="markup",
                surface_id="html",
                kind="table",
                location=HtmlElementLocation(child_indexes=(0,), tag="table"),
            ),
        ),
    )
    result = extract(data, layout=html)
    assert result.outcome == "failed" and codes(result) == [
        ("selection_unsupported", "markup")
    ]


def test_empty_selections_never_open_the_source(tmp_path):
    missing = tmp_path / "missing.pdf"
    layout = make_layout(("grid", 1, "table", (20, 40, 280, 130)))
    assert codes(extract(missing, layout=layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
    no_tables = make_layout(("photo", 1, "image", (20, 40, 280, 130)))
    result = extract(missing, layout=no_tables)
    assert result.outcome == "success" and codes(result) == [("nothing_selected", None)]

