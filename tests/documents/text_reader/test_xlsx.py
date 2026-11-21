"""Worksheet cell blocks as rows: saved results, raw values, ISO dates."""

import builtins
import datetime
import re
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.table import Table
from PIL import Image

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.layout_analysis.models import (
    XlsxDrawingLocation,
    XlsxRangeLocation,
    XlsxSheetLocation,
)
from raggae.documents.layout_analysis.region_discovery.xlsx import XlsxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import TextReader, TextReadResult, XlsxTextReader


def save(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def book(rows, title="Sheet"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    return workbook


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


def read(workbook, **kwargs):
    data = workbook if isinstance(workbook, bytes) else save(workbook)
    options = {key: kwargs.pop(key) for key in ["cell_values"] if key in kwargs}
    return XlsxTextReader(**options).read(data, **kwargs)


def texts(result):
    return [block.text for block in result.blocks]


def pages(result):
    return [(block.page_index, block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def by_region(result):
    return {block.region_id: block.text for block in result.blocks}


# Standalone reading -------------------------------------------------------


def test_xlsx_family_exports_the_same_worker():
    from raggae.documents.text_reader import xlsx as xlsx_readers

    assert xlsx_readers.XlsxTextReader is XlsxTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    workbook = book([["Part", "Price"], ["Café İstanbul", 3]])
    workbook.create_sheet("Second")["A1"] = "Two"
    data = save(workbook)
    path = tmp_path / "prices.xlsx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = XlsxTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_xlsx"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert pages(result) == [(0, "Part | Price\nCafé İstanbul | 3"), (1, "Two")]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_one_block_per_table_merged_cell_and_connected_area():
    workbook = book([])
    sheet = workbook.active
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "Price list"
    for row, values in enumerate((["Part", "Price"], ["Motor", 120], ["Gears", 45]), 3):
        for column, value in enumerate(values, 1):
            sheet.cell(row, column, value)
    sheet.add_table(Table(displayName="Parts", ref="A3:B5"))
    sheet["E3"], sheet["E4"] = "Note", "VAT excluded"
    sheet["H10"] = "Far away"
    assert texts(read(workbook)) == [
        "Part | Price\nMotor | 120\nGears | 45",
        "Price list",
        "Note\nVAT excluded",
        "Far away",
    ]


def test_empty_cells_keep_their_column_inside_a_connected_area():
    workbook = book([])
    sheet = workbook.active
    sheet["A1"], sheet["C1"] = "a", "c"
    sheet["A2"], sheet["B2"], sheet["C2"] = "d", "e", "f"
    sheet["A3"], sheet["C3"] = "x", "z"
    assert texts(read(workbook)) == ["a |  | c\nd | e | f\nx |  | z"]


def test_cells_that_touch_only_diagonally_or_across_gaps_are_separate_blocks():
    workbook = book([])
    sheet = workbook.active
    sheet["A1"], sheet["C1"] = "a", "c"
    sheet["B2"] = "b"
    sheet["A3"], sheet["B3"], sheet["C3"] = "x", "y", "z"
    assert texts(read(workbook)) == ["a", "c", " | b | \nx | y | z"]


def test_empty_rows_inside_a_declared_table_are_omitted():
    workbook = book([["Part", "Price"], [None, None], ["Motor", 120]])
    workbook.active.add_table(Table(displayName="Parts", ref="A1:B3"))
    assert texts(read(workbook)) == ["Part | Price\nMotor | 120"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, "3"),
        (3.0, "3"),
        (-0.0, "0"),
        (1234.5, "1234.5"),
        (0.1, "0.1"),
        (1e-05, "1e-05"),
        (2**53 - 1, "9007199254740991"),
        (12345678901234567890, "1.234567890123457e+19"),
        (True, "TRUE"),
        (False, "FALSE"),
        ("00123", "00123"),
        ("  padded  ", "  padded  "),
        ("#DIV/0!", "#DIV/0!"),
        ("line one\nline two\r\nthree", "line one line two three"),
        (datetime.datetime(2026, 11, 18), "2026-11-18"),
        (datetime.datetime(2026, 11, 18, 9, 30), "2026-11-18T09:30:00"),
        (
            datetime.datetime(2026, 11, 18, 9, 30, 5, 250000),
            "2026-11-18T09:30:05.250000",
        ),
        (datetime.time(9, 30), "09:30:00"),
        (datetime.timedelta(days=1, hours=2, minutes=5), "P1DT2H5M"),
        (datetime.timedelta(seconds=90), "PT1M30S"),
        (datetime.timedelta(0), "PT0S"),
    ],
)
def test_values_are_written_as_stored_with_iso_dates(value, expected):
    workbook = book([])
    workbook.active["A1"] = value
    assert texts(read(workbook)) == [expected]


def test_saved_formula_results_are_read_by_default():
    workbook = book([[5, 7, "=A1+B1"], ["=A1*2", None, None]])
    data = with_saved_results(save(workbook), {"C1": "12", "A2": "10"})
    result = read(data)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["5 | 7 | 12\n10 |  | "]


def test_formula_text_is_read_on_request():
    workbook = book([[5, 7, "=A1+B1"]])
    workbook.active["D1"] = ArrayFormula("D1:D1", "=SUM(A1:B1)")
    data = with_saved_results(save(workbook), {"C1": "12"})
    result = read(data, cell_values="formulas")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["5 | 7 | =A1+B1 | =SUM(A1:B1)"]


def test_missing_saved_results_are_reported_not_calculated():
    workbook = book([[5, 7, "=A1+B1", "=A1-B1"]])
    result = read(workbook)
    assert result.outcome == "success"
    assert texts(result) == ["5 | 7 |  | "]
    assert [(f.code, f.page_index, f.message) for f in result.findings] == [
        (
            "formula_result_missing",
            0,
            "2 formula cells have no saved result; left empty rather than calculated.",
        )
    ]


@pytest.mark.parametrize("value", ["bad", 1, None])
def test_cell_values_option_is_validated(value):
    with pytest.raises((TypeError, ValueError)):
        XlsxTextReader(cell_values=value)


def merged_report():
    workbook = book([], title="Prices")
    sheet = workbook.active
    sheet["A1"], sheet["B1"], sheet["C1"] = "Region", "Q1", "Q2"
    sheet.merge_cells("A2:A3")
    sheet["A2"] = "North"
    sheet["B2"], sheet["C2"], sheet["B3"], sheet["C3"] = 1, 2, 3, 4
    sheet.merge_cells("B4:C4")
    sheet["A4"], sheet["B4"] = "Total", 10
    return save(workbook)


def test_merged_cells_with_a_value_are_their_own_blocks_like_the_layout_map():
    # Agreed: keep the analyzer's grouping, even though it splits this table.
    assert texts(read(merged_report())) == [
        "North",
        "10",
        "Region | Q1 | Q2\n | 1 | 2\n | 3 | 4",
        "Total",
    ]


def test_a_merged_cell_inside_a_selected_range_is_read_once():
    result = XlsxTextReader().read(merged_report(), layout=sheet_layout("A1:C4"))
    assert by_region(result) == {
        "r1": "Region | Q1 | Q2\nNorth | 1 | 2\n | 3 | 4\nTotal | 10"
    }


def test_sheets_give_page_index_and_hidden_content_is_read_and_reported():
    workbook = book([["Visible"]], title="First")
    second = workbook.create_sheet("Second")
    second["A1"], second["A2"] = "Shown row", "Hidden row"
    second.row_dimensions[2].hidden = True
    third = workbook.create_sheet("Third")
    third["A1"] = "Hidden sheet"
    third.sheet_state = "hidden"
    result = read(workbook)
    assert pages(result) == [
        (0, "Visible"),
        (1, "Shown row\nHidden row"),
        (2, "Hidden sheet"),
    ]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("hidden_content", 1),
        ("hidden_content", 2),
    ]


def png():
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def test_images_charts_comments_and_chart_sheets_are_reported_not_read():
    workbook = book([["Part", "Price"], ["Motor", 120]])
    sheet = workbook.active
    sheet.add_image(SheetImage(png()), "E2")
    sheet["A1"].comment = Comment("private reviewer note", "Ada")
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=2))
    workbook.create_chartsheet("Chart").add_chart(chart)
    result = read(workbook)
    assert result.outcome == "success"
    assert texts(result) == ["Part | Price\nMotor | 120"]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("unsupported_content", 0),
        ("unsupported_content", 1),
    ]
    assert "reviewer" not in result.model_dump_json()


@pytest.mark.parametrize("rows", [[], [[None, None]]])
def test_no_cell_text_succeeds_with_an_explanation(rows):
    result = read(book(rows))
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


def test_formatting_only_cells_are_not_content():
    workbook = book([["Value"]])
    workbook.active["D9"].number_format = "0.00"
    assert texts(read(workbook)) == ["Value"]


def test_repeated_wording_is_not_deduplicated():
    workbook = book([["Same"]])
    workbook.active["C5"] = "Same"
    assert texts(read(workbook)) == ["Same", "Same"]


def test_backend_warnings_are_reported(monkeypatch):
    import warnings

    import openpyxl

    real = openpyxl.load_workbook

    def warn_then_load(*args, **kwargs):
        warnings.warn("Data Validation extension is not supported", UserWarning)
        return real(*args, **kwargs)

    import raggae.documents.text_reader.xlsx.text  # noqa: F401

    monkeypatch.setattr(openpyxl, "load_workbook", warn_then_load)
    result = read(book([["x"]]))
    assert result.outcome == "success" and texts(result) == ["x"]
    assert codes(result) == ["backend_coverage"]


# Invalid sources -----------------------------------------------------------


def zip_bytes(entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"private plain text",
        zip_bytes({"xl/workbook.xml": "<x/>"}),
        zip_bytes({"[Content_Types].xml": "<Types/>"}),
    ],
)
def test_non_xlsx_sources_fail_safely(data):
    result = XlsxTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = XlsxTextReader()
    result = reader.read(tmp_path / "private-name.xlsx")
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


def test_missing_backend_has_an_install_hint(monkeypatch):
    data = save(book([["x"]]))
    real_import = builtins.__import__

    def without_openpyxl(name, *args, **kwargs):
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_openpyxl)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        XlsxTextReader().read(data)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        XlsxTextReader().read(BytesIO(save(book([["x"]]))))


# Layout-guided reading -----------------------------------------------------


@pytest.fixture
def report():
    workbook = book([], title="Prices")
    sheet = workbook.active
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "Price list"
    for row, values in enumerate((["Part", "Price"], ["Motor", 120], ["Gears", 45]), 3):
        for column, value in enumerate(values, 1):
            sheet.cell(row, column, value)
    sheet.add_table(Table(displayName="Parts", ref="A3:B5"))
    sheet["E3"], sheet["F3"], sheet["G3"] = 5, 7, "=E3+F3"
    sheet.add_image(SheetImage(png()), "J2")
    workbook.create_sheet("Notes")["B2"] = "Prices exclude VAT"
    data = with_saved_results(save(workbook), {"G3": "12"})
    return data, XlsxLayoutAnalyzer().analyze(data)


def range_region(layout, cell_range):
    return next(
        r
        for r in layout.regions
        if isinstance(r.location, XlsxRangeLocation)
        and r.location.cell_range == cell_range
    )


def test_layout_ranges_from_the_sheet_analyzer_are_read(report):
    data, layout = report
    table, note = range_region(layout, "A3:B5"), range_region(layout, "B2")
    result = XlsxTextReader().read(data, layout=layout, region_ids=[note.id, table.id])
    assert result.outcome == "success" and not result.findings
    assert [(b.region_id, b.page_index, b.text) for b in result.blocks] == [
        (note.id, 1, "Prices exclude VAT"),
        (table.id, 0, "Part | Price\nMotor | 120\nGears | 45"),
    ]


def test_selecting_all_regions_matches_standalone_text(report):
    data, layout = report
    result = XlsxTextReader().read(data, layout=layout)
    assert result.outcome == "partial"  # The image region is rejected explicitly.
    assert sorted(texts(result)) == sorted(texts(XlsxTextReader().read(data)))
    image = next(r for r in layout.regions if r.kind == "image")
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("selection_unsupported", image.id, 0)
    ]


def sheet_layout(*ranges, sheet="Prices"):
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
                id=f"r{number}",
                surface_id="sheet-1",
                kind="unknown",
                location=XlsxRangeLocation(sheet_name=sheet, cell_range=cell_range),
            )
            for number, cell_range in enumerate(ranges, start=1)
        ),
    )


def grid():
    workbook = book([["a", "b", "c"], ["d", "e", "f"], ["g", "h", "i"]], title="Prices")
    return save(workbook)


def test_a_range_inside_another_selected_range_owns_its_cells():
    result = XlsxTextReader().read(grid(), layout=sheet_layout("A1:C3", "B2:C3"))
    assert by_region(result) == {
        "r1": "a | b | c\nd |  | \ng |  | ",
        "r2": "e | f\nh | i",
    }
    alone = XlsxTextReader().read(grid(), layout=sheet_layout("A1:C3"))
    assert by_region(alone) == {"r1": "a | b | c\nd | e | f\ng | h | i"}


@pytest.mark.parametrize("second", ["B2:D4", "A1:C3"])
def test_partly_overlapping_or_identical_ranges_are_both_rejected(second):
    result = XlsxTextReader().read(grid(), layout=sheet_layout("A1:C3", second, "A3"))
    assert codes(result)[:2] == ["selection_overlap"] * 2
    assert {f.region_id for f in result.findings[:2]} == {"r1", "r2"}


def test_a_selected_range_reads_only_its_cells_and_can_be_empty():
    result = XlsxTextReader().read(grid(), layout=sheet_layout("B1:B2", "Z99"))
    assert result.outcome == "success"
    assert by_region(result) == {"r1": "b\ne"}
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("empty", "r2", 0)
    ]


def test_missing_saved_results_are_reported_per_range():
    data = save(book([[5, "=A1*2"]], title="Prices"))
    result = XlsxTextReader().read(data, layout=sheet_layout("A1:B1"))
    assert by_region(result) == {"r1": "5 | "}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("formula_result_missing", "r1")
    ]


def test_unknown_sheet_is_not_resolved():
    result = XlsxTextReader().read(grid(), layout=sheet_layout("A1", sheet="Elsewhere"))
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("region_unresolved", "r1", None)
    ]


def test_drawing_and_non_sheet_locations_are_rejected():
    drawing = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="sheet-1",
                kind="sheet",
                location=XlsxSheetLocation(sheet_number=1, sheet_name="Prices"),
            ),
        ),
        regions=(
            LayoutRegion(
                id="chart",
                surface_id="sheet-1",
                kind="unknown",
                location=XlsxDrawingLocation(
                    sheet_name="Prices", drawing_kind="chart", drawing_index=0
                ),
            ),
        ),
    )
    result = XlsxTextReader().read(grid(), layout=drawing)
    assert result.outcome == "failed"
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("selection_unsupported", 0)
    ]
    pdf = LayoutAnalysisResult(
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
    result = XlsxTextReader().read(grid(), layout=pdf)
    assert codes(result) == ["selection_unsupported"]


def test_hidden_content_is_reported_once_per_sheet_in_region_reads():
    workbook = book([["a"], ["b"]], title="Prices")
    workbook.active.row_dimensions[2].hidden = True
    result = XlsxTextReader().read(save(workbook), layout=sheet_layout("A1", "A2"))
    assert by_region(result) == {"r1": "a", "r2": "b"}
    assert [(f.code, f.page_index) for f in result.findings] == [("hidden_content", 0)]


def test_empty_selection_reads_nothing_and_says_so(tmp_path, monkeypatch, report):
    _, layout = report
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = XlsxTextReader().read(
        tmp_path / "missing.xlsx", layout=layout, region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_layout_argument_validation(tmp_path, report):
    _, layout = report
    reader = XlsxTextReader()
    source = tmp_path / "does-not-exist"
    first = layout.regions[0].id
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=layout, region_ids=["unknown"])
    with pytest.raises(ValueError, match="duplicates"):
        reader.read(source, layout=layout, region_ids=[first, first])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")
