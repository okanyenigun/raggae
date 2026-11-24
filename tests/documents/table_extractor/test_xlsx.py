"""Tables declared in an Excel workbook, and worksheet ranges, standalone or by layout."""

import datetime
import re
import sys
from io import BytesIO
from zipfile import ZipFile

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.worksheet.table import Table

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.layout_analysis.models import (
    XlsxDrawingLocation,
    XlsxRangeLocation,
    XlsxSheetLocation,
)
from raggae.documents.layout_analysis.region_discovery.xlsx import XlsxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    HtmlTableExtractor,
    TableExtractor,
    TableExtractResult,
    XlsxTableExtractor,
)
from tests.documents.image_extractor.conftest import make_layout


def save(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def extract(workbook, **options):
    data = workbook if isinstance(workbook, bytes) else save(workbook)
    cell_values = options.pop("cell_values", "cached")
    return XlsxTableExtractor(cell_values=cell_values).extract(data, **options)


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def grid(table):
    return [
        (c.row, c.column, c.row_span, c.column_span, c.text, c.header)
        for c in table.cells
    ]


def with_saved_results(data, results, sheet_file="sheet1"):
    """Add the results Excel would have saved for formula cells, e.g. {"C1": "12"}."""
    source = ZipFile(BytesIO(data))
    output = BytesIO()
    with ZipFile(output, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == f"xl/worksheets/{sheet_file}.xml":
                text = content.decode()
                for reference, value in results.items():
                    text, count = re.subn(
                        rf'(<c r="{reference}"[^>]*>)(<f[^>]*>[^<]*</f>)<v\s*/?>(</v>)?',
                        rf"\g<1>\g<2><v>{value}</v>",
                        text,
                    )
                    assert count == 1, reference
                content = text.encode()
            target.writestr(item, content)
    return output.getvalue()


def prices():
    """A title, a declared table of parts with a total row formula, and a note beside it."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Prices"
    sheet["A1"] = "Price list"
    rows = [
        ("Part", "Price", "Since"),
        ("Motor", 120.5, datetime.date(2026, 1, 2)),
        ("Pump", 80, datetime.datetime(2025, 6, 1, 9, 30)),
        ("Total", "=B4+B5", True),
    ]
    for row_number, row in enumerate(rows, start=3):
        for column, value in enumerate(row, start=1):
            sheet.cell(row_number, column, value)
    sheet.add_table(Table(displayName="Parts", ref="A3:C6"))
    sheet["E3"] = "Note"
    return workbook


PRICES = [
    (0, 0, 1, 1, "Part", True),
    (0, 1, 1, 1, "Price", True),
    (0, 2, 1, 1, "Since", True),
    (1, 0, 1, 1, "Motor", False),
    (1, 1, 1, 1, "120.5", False),
    (1, 2, 1, 1, "2026-01-02", False),
    (2, 0, 1, 1, "Pump", False),
    (2, 1, 1, 1, "80", False),
    (2, 2, 1, 1, "2025-06-01T09:30:00", False),
    (3, 0, 1, 1, "Total", False),
    (3, 1, 1, 1, "200.5", False),
    (3, 2, 1, 1, "TRUE", False),
]


def saved_prices():
    return with_saved_results(save(prices()), {"B6": "200.5"})


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import xlsx

    assert xlsx.XlsxTableExtractor is XlsxTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = saved_prices()
    path = tmp_path / "prices.xlsx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = XlsxTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_xlsx"
    result = extractor.extract(source)
    assert result.outcome == "success"
    (table,) = result.tables
    # Stored values, never display formats: numbers as stored, dates in ISO 8601.
    assert grid(table) == PRICES
    assert (table.row_count, table.column_count) == (4, 3)
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "declared",
        0,
        None,
        None,
    )
    # The title and the note are outside the declared table.
    assert codes(result) == [("cells_outside_tables", 0)]
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_the_html_form_reads_back_into_the_same_cells():
    (table,) = extract(saved_prices()).tables
    (again,) = HtmlTableExtractor().extract(table.to_html().encode()).tables
    assert again.cells == table.cells


# Values -----------------------------------------------------------------------------------


def test_missing_saved_results_are_left_empty_and_reported():
    result = extract(prices())
    assert result.tables[0].cells[10].text == ""
    assert codes(result) == [("formula_result_missing", 0), ("cells_outside_tables", 0)]
    assert result.findings[0].message.startswith("1 formula cell has no saved result")


def test_formula_text_on_request():
    result = extract(prices(), cell_values="formulas")
    assert result.tables[0].cells[10].text == "=B4+B5"
    assert codes(result) == [("cells_outside_tables", 0)]


@pytest.mark.parametrize("value", [None, 1, "values"])
def test_cell_values_option_is_validated(value):
    with pytest.raises((TypeError, ValueError)):
        XlsxTableExtractor(cell_values=value)


def test_line_breaks_in_cells_are_kept():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Note"])
    sheet.append(["Line one\nLine two"])
    sheet.add_table(Table(displayName="Notes", ref="A1:A2"))
    assert extract(workbook).tables[0].cells[1].text == "Line one\nLine two"


# Tables -------------------------------------------------------------------------------------


def test_header_rows_follow_the_table_declaration():
    workbook = Workbook()
    sheet = workbook.active
    for row in (["a", "b"], ["c", "d"]):
        sheet.append(row)
    table = Table(displayName="NoHeader", ref="A1:B2")
    table.headerRowCount = 0
    sheet.add_table(table)
    assert not any(c.header for c in extract(workbook).tables[0].cells)


def test_tables_on_several_sheets_keep_their_sheet_position():
    workbook = prices()
    chart_source = workbook["Prices"]
    chart = BarChart()
    chart.add_data(Reference(chart_source, min_col=2, min_row=4, max_row=5))
    workbook.create_chartsheet("Chart").add_chart(chart)
    second = workbook.create_sheet("Stock")
    second.append(["Part", "Count"])
    second.append(["Motor", 3])
    second.add_table(Table(displayName="Stock", ref="A1:B2"))
    result = extract(workbook)
    # The chart sheet counts in the positions but holds no tables.
    assert [(t.page_index, t.cells[0].text) for t in result.tables] == [
        (0, "Part"),
        (2, "Part"),
    ]


# A declared table without text has empty headings, which openpyxl warns about on save.
@pytest.mark.filterwarnings("ignore:File may not be readable")
def test_workbooks_without_declared_tables_and_tables_without_text():
    workbook = Workbook()
    workbook.active.append(["loose", "cells"])
    result = extract(workbook)
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty", None), ("cells_outside_tables", 0)]
    empty = Workbook()
    empty.active.add_table(Table(displayName="Blank", ref="A1:B3"))
    assert codes(extract(empty)) == [("empty_table", 0)]


def test_hidden_content_is_read_and_reported():
    workbook = prices()
    workbook["Prices"].row_dimensions[4].hidden = True
    result = extract(saved_prices_from(workbook))
    assert grid(result.tables[0]) == PRICES
    assert ("hidden_content", 0) in codes(result)


def saved_prices_from(workbook):
    return with_saved_results(save(workbook), {"B6": "200.5"})


def test_backend_warnings_are_reported(monkeypatch):
    import warnings

    import openpyxl

    real = openpyxl.load_workbook

    def warn_then_load(*args, **kwargs):
        warnings.warn("Data Validation extension is not supported", UserWarning)
        return real(*args, **kwargs)

    monkeypatch.setattr(openpyxl, "load_workbook", warn_then_load)
    result = extract(saved_prices())
    assert result.outcome == "success" and codes(result)[-1] == ("backend_coverage", None)


# Sources -----------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"private plain text"])
def test_non_xlsx_sources_fail_safely(data):
    result = extract(data)
    assert result.outcome == "failed" and codes(result) == [("decode_failed", None)]
    assert "private" not in result.model_dump_json()


def test_missing_files_and_missing_openpyxl(tmp_path, monkeypatch):
    assert codes(XlsxTableExtractor().extract(tmp_path / "missing.xlsx")) == [
        ("source_unreadable", None)
    ]
    data = saved_prices()
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        XlsxTableExtractor().extract(data)


# Layout regions -----------------------------------------------------------------------------


def summary():
    """A sheet without declared tables: "North" merged down two rows."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Summary"
    for row in (["Region", "Year", "Units"], ["North", 2025, 1200], [None, 2026, 1350]):
        sheet.append(row)
    sheet.merge_cells("A2:A3")
    return save(workbook)


def sheet_layout(*ranges, sheet="Summary"):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="sheet-1",
                kind="sheet",
                location=XlsxSheetLocation(sheet_number=1, sheet_name=sheet),
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=identifier,
                surface_id="sheet-1",
                kind="unknown",
                location=XlsxRangeLocation(sheet_name=sheet, cell_range=cell_range),
            )
            for identifier, cell_range in ranges
        ),
    )


def ranges(data, layout):
    """Every region of a hand-made layout, asked for by ID (they are not table regions)."""
    return extract(data, layout=layout, region_ids=[r.id for r in layout.regions])


def test_layout_links_each_declared_table_to_its_table_region():
    data = saved_prices()
    layout = XlsxLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    result = extract(data, layout=layout)
    assert [t.region_id for t in result.tables] == [region.id]
    assert grid(result.tables[0]) == PRICES
    assert result.outcome == "success" and not result.findings


def test_a_range_asked_for_by_id_is_read_as_a_grid():
    data = summary()
    layout = XlsxLayoutAnalyzer().analyze(data)
    (block,) = [r for r in layout.regions if r.location.cell_range == "A1:C3"]
    assert block.kind == "unknown"
    assert codes(extract(data, layout=layout)) == [("nothing_selected", None)]
    (table,) = extract(data, layout=layout, region_ids=[block.id]).tables
    # No declared table, so no header cells; the merge is one cell with its span.
    assert grid(table) == [
        (0, 0, 1, 1, "Region", False),
        (0, 1, 1, 1, "Year", False),
        (0, 2, 1, 1, "Units", False),
        (1, 0, 2, 1, "North", False),
        (1, 1, 1, 1, "2025", False),
        (1, 2, 1, 1, "1200", False),
        (2, 1, 1, 1, "2026", False),
        (2, 2, 1, 1, "1350", False),
    ]


def test_header_cells_follow_a_declared_table_inside_a_wider_range():
    data = saved_prices()
    (table,) = ranges(data, sheet_layout(("wide", "A1:C6"), sheet="Prices")).tables
    assert [(c.row, c.text) for c in table.cells if c.header] == [
        (2, "Part"),
        (2, "Price"),
        (2, "Since"),
    ]


def test_merges_cut_by_a_range_and_merges_from_outside():
    data = summary()
    cut = ranges(data, sheet_layout(("top", "A1:C2")))
    assert grid(cut.tables[0])[3] == (1, 0, 1, 1, "North", False)
    assert codes(cut) == [("span_shortened", 0)]
    # The merge starts above this range, so the position it covers is left uncovered.
    below = ranges(data, sheet_layout(("bottom", "A3:C3")))
    assert [(c.column, c.text) for c in below.tables[0].cells] == [(1, "2026"), (2, "1350")]


def test_an_inner_range_owns_its_cells_and_crossing_ranges_are_refused():
    data = summary()
    nested = ranges(data, sheet_layout(("outer", "A1:C3"), ("inner", "B2:C3")))
    outer, inner = nested.tables
    assert [c.text for c in outer.cells if c.row > 0 and c.column > 0] == ["", "", "", ""]
    assert [c.text for c in inner.cells] == ["2025", "1200", "2026", "1350"]
    crossing = ranges(data, sheet_layout(("left", "A1:B3"), ("right", "B1:C3")))
    assert crossing.outcome == "failed"
    assert [(f.code, f.region_id) for f in crossing.findings] == [
        ("selection_overlap", "left"),
        ("selection_overlap", "right"),
    ]


def test_ranges_without_text_unknown_sheets_and_drawings():
    data = summary()
    empty = ranges(data, sheet_layout(("blank", "E5:F6")))
    assert empty.outcome == "success" and codes(empty) == [("empty_table", 0)]
    ghost = ranges(data, sheet_layout(("ghost", "A1:B2"), sheet="Ghost"))
    assert ghost.outcome == "failed"
    assert [(f.code, f.region_id) for f in ghost.findings] == [("region_unresolved", "ghost")]
    drawing = LayoutAnalysisResult(
        outcome="success",
        surfaces=sheet_layout().surfaces,
        regions=(
            LayoutRegion(
                id="chart",
                surface_id="sheet-1",
                kind="image",
                location=XlsxDrawingLocation(
                    sheet_name="Summary", drawing_kind="chart", drawing_index=0
                ),
            ),
        ),
    )
    result = extract(data, layout=drawing, region_ids=["chart"])
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "chart")
    ]


def test_non_sheet_locations_and_empty_selections(tmp_path):
    pdf_layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = extract(summary(), layout=pdf_layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "grid")
    ]
    missing = tmp_path / "missing.xlsx"
    assert codes(XlsxTableExtractor().extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
