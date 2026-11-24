"""Tables declared on PowerPoint slides, standalone or by layout region."""

import sys
from io import BytesIO

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from raggae.documents.layout_analysis import LayoutAnalysisResult, LayoutRegion
from raggae.documents.layout_analysis.models import PptxShapeLocation
from raggae.documents.layout_analysis.region_discovery.pptx import PptxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    HtmlTableExtractor,
    PptxTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout


def save(presentation):
    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def extract(deck, **options):
    data = deck if isinstance(deck, bytes) else save(deck)
    return PptxTableExtractor().extract(data, **options)


def codes(result):
    return [(f.code, f.region_id) for f in result.findings]


def grid(table):
    return [
        (c.row, c.column, c.row_span, c.column_span, c.text, c.header)
        for c in table.cells
    ]


def blank(presentation):
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def add_table(slide, rows, columns):
    return slide.shapes.add_table(
        rows, columns, Inches(1), Inches(1), Inches(6), Inches(2)
    )


def prices():
    """A title slide and a price slide: "Part" merged down two rows, "Price" across
    two columns, a cell with two paragraphs."""
    presentation = Presentation()
    blank(presentation).shapes.add_textbox(
        Inches(1), Inches(1), Inches(4), Inches(1)
    ).text_frame.text = "Price list"
    table = add_table(blank(presentation), 4, 3).table
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 0).text = "Part"
    table.cell(0, 1).merge(table.cell(0, 2))
    table.cell(0, 1).text = "Price"
    table.cell(1, 1).text, table.cell(1, 2).text = "2025", "2026"
    for column, value in enumerate(("Motor", "110", "120")):
        table.cell(2, column).text = value
    frame = table.cell(3, 0).text_frame
    frame.text = "Stand"
    frame.add_paragraph().text = "with base plate"
    return presentation


# python-pptx turns PowerPoint's first-row flag on, so row 0 holds header cells.
PRICES = [
    (0, 0, 2, 1, "Part", True),
    (0, 1, 1, 2, "Price", True),
    (1, 1, 1, 1, "2025", False),
    (1, 2, 1, 1, "2026", False),
    (2, 0, 1, 1, "Motor", False),
    (2, 1, 1, 1, "110", False),
    (2, 2, 1, 1, "120", False),
    (3, 0, 1, 1, "Stand\nwith base plate", False),
    (3, 1, 1, 1, "", False),
    (3, 2, 1, 1, "", False),
]


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import pptx

    assert pptx.PptxTableExtractor is PptxTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = save(prices())
    path = tmp_path / "prices.pptx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = PptxTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_pptx"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    assert grid(table) == PRICES
    assert (table.row_count, table.column_count) == (4, 3)
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "declared",
        1,
        None,
        None,
    )
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_the_html_form_reads_back_into_the_same_cells():
    (table,) = extract(prices()).tables
    (again,) = HtmlTableExtractor().extract(table.to_html().encode()).tables
    assert again.cells == table.cells


# The grid ------------------------------------------------------------------------------


def test_header_flags_make_header_cells():
    presentation = Presentation()
    table = add_table(blank(presentation), 2, 2).table
    for (row, column), value in {(0, 0): "", (0, 1): "B", (1, 0): "Row", (1, 1): "1"}.items():
        table.cell(row, column).text = value
    properties = table._tbl.tblPr
    properties.set("firstRow", "0")
    properties.set("firstCol", "1")
    (extracted,) = extract(presentation).tables
    assert [(c.text, c.header) for c in extracted.cells] == [
        ("", True),
        ("B", False),
        ("Row", True),
        ("1", False),
    ]


def test_tables_in_groups_and_on_every_slide_in_order():
    presentation = Presentation()
    first = add_table(blank(presentation), 1, 1)
    first.table.cell(0, 0).text = "loose"
    slide = blank(presentation)
    frame = add_table(slide, 1, 1)
    frame.table.cell(0, 0).text = "grouped"
    slide.shapes.add_group_shape()._element.append(frame._element)
    later = add_table(slide, 1, 1)
    later.table.cell(0, 0).text = "after the group"
    result = extract(presentation)
    assert [(t.page_index, t.cells[0].text) for t in result.tables] == [
        (0, "loose"),
        (1, "grouped"),
        (1, "after the group"),
    ]


def test_hidden_slides_are_read_and_reported():
    presentation = prices()
    presentation.slides[1]._element.set("show", "0")
    result = extract(presentation)
    assert grid(result.tables[0]) == PRICES
    assert result.outcome == "success" and codes(result) == [("hidden_slide", None)]
    assert result.findings[0].page_index == 1


def test_captions_come_from_the_alt_text():
    presentation = Presentation()
    frame = add_table(blank(presentation), 1, 1)
    frame.table.cell(0, 0).text = "a"
    names = frame._element.nvGraphicFramePr.cNvPr
    names.set("descr", "Long text")
    assert extract(presentation).tables[0].caption == "Long text"
    names.set("title", "Prices")
    assert extract(presentation).tables[0].caption == "Prices"


def test_text_in_covered_positions_is_kept():
    presentation = Presentation()
    table = add_table(blank(presentation), 1, 3).table
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "Merged"
    table.cell(0, 1).text = "hidden under the merge"
    cells = table._tbl.tr_lst[0].tc_lst
    cells[2].set("hMerge", "1")  # covered, but no merge reaches it
    table.cell(0, 2).text = "stray"
    (extracted,) = extract(presentation).tables
    assert [(c.column, c.column_span, c.text) for c in extracted.cells] == [
        (0, 2, "Merged\nhidden under the merge"),
        (2, 1, "stray"),
    ]


def test_a_span_past_the_grid_is_shortened():
    presentation = Presentation()
    table = add_table(blank(presentation), 2, 2).table
    table.cell(0, 0).text, table.cell(1, 1).text = "wide", "x"
    table._tbl.tr_lst[0].tc_lst[0].set("gridSpan", "5")
    table._tbl.tr_lst[0].tc_lst[1].set("hMerge", "1")
    result = extract(presentation)
    assert [(c.row, c.column, c.column_span) for c in result.tables[0].cells][0] == (0, 0, 2)
    assert codes(result) == [("span_shortened", None)]


def test_tables_without_text_and_decks_without_tables():
    presentation = Presentation()
    add_table(blank(presentation), 2, 2)
    result = extract(presentation)
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty_table", None)]
    assert result.findings[0].page_index == 0
    plain = Presentation()
    blank(plain)
    result = extract(plain)
    assert codes(result) == [("empty", None)] and result.findings[0].page_index is None


# Sources -----------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"private plain text"])
def test_non_pptx_sources_fail_safely(data):
    result = extract(data)
    assert result.outcome == "failed" and codes(result) == [("decode_failed", None)]
    assert "private" not in result.model_dump_json()


def test_missing_files_and_missing_python_pptx(tmp_path, monkeypatch):
    assert codes(PptxTableExtractor().extract(tmp_path / "missing.pptx")) == [
        ("source_unreadable", None)
    ]
    data = save(prices())
    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        PptxTableExtractor().extract(data)


def test_too_deep_groups_are_reported(monkeypatch):
    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(PptxTableExtractor, "_tables", staticmethod(too_deep))
    result = extract(prices())
    assert result.outcome == "failed" and codes(result) == [("structure_unsupported", None)]


# Layout regions -----------------------------------------------------------------------------


def grouped_deck():
    """Slide 1: a text box, a chart, and a group holding a table."""
    presentation = Presentation()
    slide = blank(presentation)
    slide.shapes.add_textbox(Inches(1), Inches(0.2), Inches(4), Inches(0.5)).text_frame.text = (
        "Kits"
    )
    chart = CategoryChartData()
    chart.categories = ["North", "South"]
    chart.add_series("Units", (12, 9))
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(3), Inches(2), chart
    )
    frame = add_table(slide, 1, 2)
    frame.table.cell(0, 0).text, frame.table.cell(0, 1).text = "Kit", "bolts"
    slide.shapes.add_group_shape()._element.append(frame._element)
    return save(presentation)


def test_layout_links_each_table_to_its_table_region():
    data = save(prices())
    layout = PptxLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    result = extract(data, layout=layout)
    assert [t.region_id for t in result.tables] == [region.id]
    assert grid(result.tables[0]) == PRICES
    assert result.outcome == "success" and not result.findings


def shape_layout(data, *regions):
    layout = PptxLayoutAnalyzer().analyze(data)
    surface = layout.surfaces[0]
    part = surface.location.part
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=layout.surfaces,
        regions=tuple(
            LayoutRegion(
                id=identifier,
                surface_id=surface.id_,
                kind=kind,
                location=PptxShapeLocation(part=part, shape_id=shape_id, shape_path=path),
            )
            for identifier, kind, shape_id, path in regions
        ),
    )


def shape_ids(data):
    presentation = Presentation(BytesIO(data))
    shapes = list(presentation.slides[0].shapes)
    return [shape.shape_id for shape in shapes], list(shapes[2].shapes)[0].shape_id


def test_a_group_region_gives_its_tables_and_the_innermost_region_owns_them():
    data = grouped_deck()
    (text, chart, group), table = shape_ids(data)
    layout = shape_layout(
        data,
        ("group", "unknown", group, (2,)),
        ("table", "table", table, (2, 0)),
        ("text", "text", text, (0,)),
    )
    alone = extract(data, layout=layout, region_ids=["group"])
    assert [(t.region_id, t.cells[0].text) for t in alone.tables] == [("group", "Kit")]
    both = extract(data, layout=layout, region_ids=["group", "table", "text"])
    assert [t.region_id for t in both.tables] == ["table"]
    assert codes(both) == [("empty", "group"), ("empty", "text")]


def test_charts_unresolved_and_overlapping_regions():
    data = grouped_deck()
    (text, chart, group), table = shape_ids(data)
    layout = shape_layout(
        data,
        ("chart", "unknown", chart, (1,)),
        ("wrong-id", "table", 999, (2, 0)),
        ("far", "table", table, (7,)),
        ("twin-a", "table", table, (2, 0)),
        ("twin-b", "table", table, (2, 0)),
    )
    result = extract(data, layout=layout, region_ids=[r.id for r in layout.regions])
    assert result.outcome == "failed" and not result.tables
    assert codes(result) == [
        ("selection_unsupported", "chart"),
        ("region_unresolved", "wrong-id"),
        ("region_unresolved", "far"),
        ("selection_overlap", "twin-a"),
        ("selection_overlap", "twin-b"),
    ]


def test_non_slide_locations_and_empty_selections(tmp_path):
    pdf_layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = extract(prices(), layout=pdf_layout)
    assert result.outcome == "failed" and codes(result) == [("selection_unsupported", "grid")]
    missing = tmp_path / "missing.pptx"
    assert codes(PptxTableExtractor().extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
    no_tables = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    result = PptxTableExtractor().extract(missing, layout=no_tables)
    assert result.outcome == "success" and codes(result) == [("nothing_selected", None)]
