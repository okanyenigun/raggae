"""Worksheet rows as XML: sheet, row number, and cells named by table column or letter."""

import datetime
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from zipfile import ZipFile

import pytest
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.table import Table
from PIL import Image

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.layout_analysis.models import (
    XlsxRangeLocation,
    XlsxSheetLocation,
)
from raggae.documents.layout_analysis.region_discovery.xlsx import XlsxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    TextContentFormat,
    TextReader,
    TextReadResult,
    XlsxTextReader,
    XlsxXmlTextReader,
)


def save(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def book(rows, title="Prices"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    return workbook


def with_saved_results(data, results):
    source = ZipFile(BytesIO(data))
    output = BytesIO()
    with ZipFile(output, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
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
    return XlsxXmlTextReader(**options).read(data, **kwargs)


def rows(result):
    """Each block parsed as XML: (attributes, [(tag, text), ...]) per row."""
    parsed = []
    for block in result.blocks:
        root = ET.fromstring(block.text)
        assert root.tag == "row"
        parsed.append((dict(root.attrib), [(c.tag, c.text or "") for c in root]))
    return parsed


def codes(result):
    return [finding.code for finding in result.findings]


def table_book():
    workbook = book([["Part", "Price", "Since"]])
    sheet = workbook.active
    sheet.append(["Motor", 120.5, datetime.datetime(2026, 1, 2)])
    sheet.append(["Gears", 45, None])
    sheet.add_table(Table(displayName="Parts", ref="A1:C3"))
    return workbook


def with_illegal_character(monkeypatch, marker="BAD"):
    """Cell text holding a control character; real files cannot (XML 1.0 forbids it)."""
    from raggae.documents.text_reader.xlsx import book as workbook_rows
    from raggae.documents.text_reader.xlsx import values

    real = workbook_rows.Book.rows

    def rows_with_control_character(self, name, box, owned):
        for row, cells in real(self, name, box, owned):
            yield (
                row,
                [
                    values.Cell(cell.column, cell.value.replace(marker, "\x0b"))
                    if isinstance(cell.value, str) and marker in cell.value
                    else cell
                    for cell in cells
                ],
            )

    monkeypatch.setattr(workbook_rows.Book, "rows", rows_with_control_character)


# Standalone reading -------------------------------------------------------


def test_xlsx_family_exports_the_same_worker():
    from raggae.documents.text_reader import xlsx as xlsx_readers

    assert xlsx_readers.XlsxXmlTextReader is XlsxXmlTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    workbook = book([["Café İstanbul", 3]])
    workbook.create_sheet("Second")["B2"] = "Two"
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
    reader = XlsxXmlTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_xlsx_xml"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, a, c) for b, (a, c) in zip(result.blocks, rows(result))] == [
        (0, {"sheet": "Prices", "row": "1"}, [("A", "Café İstanbul"), ("B", "3")]),
        (1, {"sheet": "Second", "row": "2"}, [("B", "Two")]),
    ]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native"
        assert block.content_format is TextContentFormat.XML
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_output_is_compact_with_attributes_then_cells_in_order():
    result = read(book([["x", 2]]))
    assert result.blocks[0].text == '<row sheet="Prices" row="1"><A>x</A><B>2</B></row>'


def test_declared_tables_use_column_names_and_skip_the_header_row():
    result = read(table_book())
    assert rows(result) == [
        (
            {"sheet": "Prices", "row": "2", "table": "Parts"},
            [("Part", "Motor"), ("Price", "120.5"), ("Since", "2026-01-02")],
        ),
        (
            {"sheet": "Prices", "row": "3", "table": "Parts"},
            [("Part", "Gears"), ("Price", "45")],
        ),
    ]
    assert result.outcome == "success" and not result.findings


def test_a_table_without_a_header_row_uses_its_stored_generic_names():
    workbook = table_book()
    workbook.active.tables["Parts"].headerRowCount = 0
    result = read(workbook)
    assert [attributes["row"] for attributes, _ in rows(result)] == ["1", "2", "3"]
    assert [tag for tag, _ in rows(result)[0][1]] == ["Column1", "Column2", "Column3"]


def test_cells_outside_tables_are_named_by_column_letter_without_guessing_headers():
    workbook = book([])
    sheet = workbook.active
    sheet["AA5"], sheet["AB5"] = "Name", "City"
    sheet["AA6"], sheet["AB6"] = "Ada", "London"
    assert rows(read(workbook)) == [
        ({"sheet": "Prices", "row": "5"}, [("AA", "Name"), ("AB", "City")]),
        ({"sheet": "Prices", "row": "6"}, [("AA", "Ada"), ("AB", "London")]),
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, "3"),
        (3.0, "3"),
        (1234.5, "1234.5"),
        (0.1, "0.1"),
        (2**53 - 1, str(2**53 - 1)),
        (12345678901234567890, "1.234567890123457e+19"),
        (True, "true"),
        (False, "false"),
        ("00123", "00123"),
        ("  padded\nline\ttab  ", "  padded\nline\ttab  "),
        ("#DIV/0!", "#DIV/0!"),
        (datetime.datetime(2026, 11, 18), "2026-11-18"),
        (datetime.datetime(2026, 11, 18, 9, 30), "2026-11-18T09:30:00"),
        (datetime.time(9, 30), "09:30:00"),
        (datetime.timedelta(days=1, hours=2, minutes=5), "P1DT2H5M"),
    ],
)
def test_values_are_written_as_text(value, expected):
    workbook = book([])
    workbook.active["A1"] = value
    assert rows(read(workbook))[0][1] == [("A", expected)]


def test_markup_characters_are_escaped_in_cells_and_attributes():
    workbook = book(
        [["a & b < c > d", "\"quoted\" 'single' &amp; <tag>"]], title='R&D "1" <x>'
    )
    result = read(workbook)
    assert rows(result) == [
        (
            {"sheet": 'R&D "1" <x>', "row": "1"},
            [("A", "a & b < c > d"), ("B", "\"quoted\" 'single' &amp; <tag>")],
        )
    ]
    assert "<tag>" not in result.blocks[0].text


def test_a_table_name_is_escaped_too():
    workbook = book([["K", "V"], ["x", "y"]])
    workbook.active.add_table(Table(displayName="Shipping", ref="A1:B2"))
    assert rows(read(workbook))[0][0]["table"] == "Shipping"


def test_empty_cells_are_left_out_and_empty_rows_give_no_block():
    workbook = book([["a", None, "c"]])
    sheet = workbook.active
    sheet["A2"], sheet["B2"], sheet["C2"] = "d", "e", "f"
    sheet.add_table(Table(displayName="Wide", ref="E1:F3"))
    sheet["E1"], sheet["F1"] = "K", "V"
    sheet["E3"] = "x"
    assert rows(read(workbook)) == [
        ({"sheet": "Prices", "row": "3", "table": "Wide"}, [("K", "x")]),
        ({"sheet": "Prices", "row": "1"}, [("A", "a"), ("C", "c")]),
        ({"sheet": "Prices", "row": "2"}, [("A", "d"), ("B", "e"), ("C", "f")]),
    ]


def test_saved_formula_results_are_read_by_default():
    data = with_saved_results(save(book([[5, 7, "=A1+B1"]])), {"C1": "12"})
    result = read(data)
    assert result.outcome == "success" and not result.findings
    assert rows(result)[0][1] == [("A", "5"), ("B", "7"), ("C", "12")]


def test_formula_text_is_read_on_request():
    workbook = book([[5, 7, "=A1+B1"]])
    workbook.active["D1"] = ArrayFormula("D1:D1", "=SUM(A1:B1)")
    result = read(workbook, cell_values="formulas")
    assert result.outcome == "success" and not result.findings
    assert rows(result)[0][1] == [
        ("A", "5"),
        ("B", "7"),
        ("C", "=A1+B1"),
        ("D", "=SUM(A1:B1)"),
    ]


def test_missing_saved_results_are_empty_elements_and_reported():
    result = read(book([[5, "=A1*2", None, "=A1*3"]]))
    assert result.outcome == "success"
    # The empty C1 makes D1 its own cell group, so row 1 gives two blocks.
    assert [cells for _, cells in rows(result)] == [
        [("A", "5"), ("B", "")],
        [("D", "")],
    ]
    assert result.blocks[0].text.endswith("<B/></row>")
    assert [(f.code, f.page_index, f.message) for f in result.findings] == [
        (
            "formula_result_missing",
            0,
            "2 formula cells have no saved result; written as an empty element "
            "rather than calculated.",
        )
    ]


def test_a_row_of_only_missing_results_is_still_emitted():
    result = read(book([["=1+1"]]))
    assert [b.text for b in result.blocks] == ['<row sheet="Prices" row="1"><A/></row>']


def test_merged_cells_appear_once_at_their_top_left_address():
    workbook = book([])
    sheet = workbook.active
    sheet.merge_cells("B2:D2")
    sheet["B2"] = "Title"
    assert rows(read(workbook)) == [({"sheet": "Prices", "row": "2"}, [("B", "Title")])]


def test_grouping_page_index_and_notes_match_the_text_reader():
    workbook = table_book()
    sheet = workbook.active
    sheet["F8"] = "Note"
    sheet.row_dimensions[3].hidden = True
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    buffer.seek(0)
    sheet.add_image(SheetImage(buffer), "H2")
    workbook.create_sheet("Second")["A1"] = "Two"
    data = save(workbook)
    as_xml = XlsxXmlTextReader().read(data)
    as_text = XlsxTextReader().read(data)
    assert [b.page_index for b in as_xml.blocks] == [0, 0, 0, 1]
    assert codes(as_xml) == codes(as_text)


def test_sheet_position_is_page_index_even_after_an_empty_sheet():
    workbook = book([["first"]], title="One")
    workbook.create_sheet("Empty")
    workbook.create_sheet("Third")["A1"] = "third"
    assert [(b.page_index) for b in read(workbook).blocks] == [0, 2]


# Names that are not XML tags, and characters XML cannot hold ---------------


def odd_table_book():
    workbook = book([["Unit price", "2024"], ["x", "y"]])
    workbook.active.add_table(Table(displayName="Odd", ref="A1:B2"))
    return workbook


def test_a_table_column_name_that_is_not_a_tag_makes_its_rows_use_letters():
    result = read(odd_table_book())
    assert result.outcome == "success"
    assert rows(result) == [
        ({"sheet": "Prices", "row": "2", "table": "Odd"}, [("A", "x"), ("B", "y")])
    ]
    assert [(f.code, f.page_index) for f in result.findings] == [("header_invalid", 0)]
    assert "Unit price" not in result.model_dump_json()


def test_valid_names_in_one_table_are_unaffected_by_an_odd_table_elsewhere():
    workbook = odd_table_book()
    sheet = workbook.active
    sheet["E1"], sheet["F1"] = "Part", "Cost"
    sheet["E2"], sheet["F2"] = "Motor", 3
    sheet.add_table(Table(displayName="Good", ref="E1:F2"))
    result = read(workbook)
    assert [a.get("table") for a, _ in rows(result)] == ["Odd", "Good"]
    assert rows(result)[1][1] == [("Part", "Motor"), ("Cost", "3")]
    assert codes(result) == ["header_invalid"]


def test_a_row_holding_a_character_xml_cannot_represent_is_left_out(monkeypatch):
    with_illegal_character(monkeypatch)
    result = read(book([["ok", 1], ["BAD", 2], ["fine", 3]]))
    assert result.outcome == "partial"
    assert [cells for _, cells in rows(result)] == [
        [("A", "ok"), ("B", "1")],
        [("A", "fine"), ("B", "3")],
    ]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("invalid_character", 0)
    ]
    assert "left out" in result.findings[0].message
    assert (
        "\x0b" not in result.model_dump_json() and "BAD" not in result.model_dump_json()
    )


def test_other_rows_survive_and_each_skipped_row_is_counted(monkeypatch):
    with_illegal_character(monkeypatch)
    result = read(book([["BAD1"], ["ok"], ["BAD2"]]))
    assert [cells for _, cells in rows(result)] == [[("A", "ok")]]
    assert result.findings[0].message.startswith("2 rows hold a character")


def test_a_sheet_where_every_row_is_left_out_fails(monkeypatch):
    with_illegal_character(monkeypatch)
    result = read(book([["BAD"]]))
    assert result.outcome == "failed" and not result.blocks
    # `empty` is the shared note for a workbook that yielded no blocks.
    assert codes(result) == ["invalid_character", "empty"]


# Sources and configuration -------------------------------------------------


def test_no_cell_text_succeeds_with_an_explanation():
    result = read(Workbook())
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


@pytest.mark.parametrize("data", [b"private text", b"", b"%PDF-1.4"])
def test_not_a_workbook_fails_safely(data):
    result = XlsxXmlTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_files_are_reported_without_the_path(tmp_path):
    result = XlsxXmlTextReader().read(tmp_path / "private-name.xlsx")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()


def test_configuration_and_source_types():
    with pytest.raises(ValueError):
        XlsxXmlTextReader(cell_values="values")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        XlsxXmlTextReader(cell_values=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source must"):
        XlsxXmlTextReader().read(BytesIO(save(Workbook())))  # type: ignore[arg-type]


# Layout-guided reading -----------------------------------------------------


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


def test_layout_regions_give_one_block_per_row_with_region_ids():
    data = save(table_book())
    layout = XlsxLayoutAnalyzer().analyze(data)
    result = XlsxXmlTextReader().read(data, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert {block.region_id for block in result.blocks} == {layout.regions[0].id}
    assert [a["row"] for a, _ in rows(result)] == ["2", "3"]


def test_a_layout_without_region_ids_reads_every_region_and_ids_select():
    data = save(book([["a", "b"], [None, None], ["c", "d"]]))
    layout = sheet_layout("A1:B1", "A3:B3")
    everything = XlsxXmlTextReader().read(data, layout=layout)
    assert [b.region_id for b in everything.blocks] == ["r1", "r2"]
    chosen = XlsxXmlTextReader().read(data, layout=layout, region_ids=["r2"])
    assert [(b.region_id, c) for b, (_, c) in zip(chosen.blocks, rows(chosen))] == [
        ("r2", [("A", "c"), ("B", "d")])
    ]


def test_a_range_covering_part_of_a_table_still_uses_its_names():
    result = XlsxXmlTextReader().read(save(table_book()), layout=sheet_layout("B1:B3"))
    assert rows(result) == [
        ({"sheet": "Prices", "row": "2", "table": "Parts"}, [("Price", "120.5")]),
        ({"sheet": "Prices", "row": "3", "table": "Parts"}, [("Price", "45")]),
    ]


def test_a_row_mixing_table_and_outside_cells_uses_letters_and_no_table():
    workbook = table_book()
    workbook.active["E2"] = "outside"
    result = XlsxXmlTextReader().read(save(workbook), layout=sheet_layout("A2:E2"))
    assert rows(result) == [
        (
            {"sheet": "Prices", "row": "2"},
            [("A", "Motor"), ("B", "120.5"), ("C", "2026-01-02"), ("E", "outside")],
        )
    ]


def test_inner_ranges_own_their_cells_and_empty_ranges_are_reported():
    data = save(book([["a", "b"], ["c", "d"]]))
    result = XlsxXmlTextReader().read(data, layout=sheet_layout("A1:B2", "B2", "Z9"))
    assert [
        (b.region_id, cells) for b, (_, cells) in zip(result.blocks, rows(result))
    ] == [
        ("r1", [("A", "a"), ("B", "b")]),
        ("r1", [("A", "c")]),
        ("r2", [("B", "d")]),
    ]
    assert [(f.code, f.region_id) for f in result.findings] == [("empty", "r3")]


def test_missing_results_are_reported_per_range():
    data = save(book([[5, "=A1*2"]]))
    result = XlsxXmlTextReader().read(data, layout=sheet_layout("A1:B1"))
    assert rows(result)[0][1] == [("A", "5"), ("B", "")]
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("formula_result_missing", "r1")
    ]


def test_odd_table_names_are_reported_per_range():
    result = XlsxXmlTextReader().read(
        save(odd_table_book()), layout=sheet_layout("A1:B2")
    )
    assert result.outcome == "success"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("header_invalid", "r1")
    ]


def test_illegal_characters_are_reported_per_range_and_make_it_partial(monkeypatch):
    with_illegal_character(monkeypatch)
    data = save(book([["ok"], ["BAD"]]))
    result = XlsxXmlTextReader().read(data, layout=sheet_layout("A1:A2"))
    assert result.outcome == "partial"
    assert [cells for _, cells in rows(result)] == [[("A", "ok")]]
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("invalid_character", "r1")
    ]


def test_partly_overlapping_ranges_are_both_rejected():
    data = save(book([["a", "b"], ["c", "d"]]))
    result = XlsxXmlTextReader().read(data, layout=sheet_layout("A1:B1", "B1:B2"))
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["selection_overlap"] * 2


def test_empty_selection_reads_nothing_and_says_so(tmp_path):
    result = XlsxXmlTextReader().read(
        tmp_path / "missing.xlsx", layout=sheet_layout("A1"), region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]
