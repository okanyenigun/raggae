"""Worksheet rows as JSON: sheet, row number, typed cells keyed by table or letter."""

import datetime
import json
import re
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
    TextReader,
    TextReadResult,
    XlsxJsonTextReader,
    XlsxTextReader,
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
    return XlsxJsonTextReader(**options).read(data, **kwargs)


def rows(result):
    return [json.loads(block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def table_book():
    workbook = book([["Part", "Price", "Since"]])
    sheet = workbook.active
    sheet.append(["Motor", 120.5, datetime.datetime(2026, 1, 2)])
    sheet.append(["Gears", 45, None])
    sheet.add_table(Table(displayName="Parts", ref="A1:C3"))
    return workbook


# Standalone reading -------------------------------------------------------


def test_xlsx_family_exports_the_same_worker():
    from raggae.documents.text_reader import xlsx as xlsx_readers

    assert xlsx_readers.XlsxJsonTextReader is XlsxJsonTextReader


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
    reader = XlsxJsonTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_xlsx_json"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, json.loads(b.text)) for b in result.blocks] == [
        (0, {"sheet": "Prices", "row": 1, "cells": {"A": "Café İstanbul", "B": 3}}),
        (1, {"sheet": "Second", "row": 2, "cells": {"B": "Two"}}),
    ]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native" and block.content_format == "json"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_output_is_compact_json_with_keys_in_order():
    result = read(book([["x", 2]]))
    assert result.blocks[0].text == '{"sheet":"Prices","row":1,"cells":{"A":"x","B":2}}'


def test_declared_tables_use_column_names_and_skip_the_header_row():
    result = read(table_book())
    assert rows(result) == [
        {
            "sheet": "Prices",
            "row": 2,
            "table": "Parts",
            "cells": {"Part": "Motor", "Price": 120.5, "Since": "2026-01-02"},
        },
        {
            "sheet": "Prices",
            "row": 3,
            "table": "Parts",
            "cells": {"Part": "Gears", "Price": 45},
        },
    ]


def test_a_table_without_a_header_row_uses_its_stored_generic_names():
    # With no header row, Excel and openpyxl store generic column names.
    workbook = table_book()
    workbook.active.tables["Parts"].headerRowCount = 0
    result = read(workbook)
    assert [row["row"] for row in rows(result)] == [1, 2, 3]
    assert rows(result)[0]["cells"] == {
        "Column1": "Part",
        "Column2": "Price",
        "Column3": "Since",
    }


def test_cells_outside_tables_are_keyed_by_column_letter_without_guessing_headers():
    workbook = book([])
    sheet = workbook.active
    sheet["AA5"], sheet["AB5"] = "Name", "City"
    sheet["AA6"], sheet["AB6"] = "Ada", "London"
    assert rows(read(workbook)) == [
        {"sheet": "Prices", "row": 5, "cells": {"AA": "Name", "AB": "City"}},
        {"sheet": "Prices", "row": 6, "cells": {"AA": "Ada", "AB": "London"}},
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3),
        (3.0, 3),
        (1234.5, 1234.5),
        (0.1, 0.1),
        (2**53 - 1, 2**53 - 1),
        (12345678901234567890, 1.234567890123457e19),
        (True, True),
        (False, False),
        ("00123", "00123"),
        ("  padded\nline  ", "  padded\nline  "),
        ("#DIV/0!", "#DIV/0!"),
        (datetime.datetime(2026, 11, 18), "2026-11-18"),
        (datetime.datetime(2026, 11, 18, 9, 30), "2026-11-18T09:30:00"),
        (datetime.time(9, 30), "09:30:00"),
        (datetime.timedelta(days=1, hours=2, minutes=5), "P1DT2H5M"),
    ],
)
def test_values_keep_their_types(value, expected):
    workbook = book([])
    workbook.active["A1"] = value
    cell = rows(read(workbook))[0]["cells"]["A"]
    assert cell == expected and type(cell) is type(expected)


def test_large_whole_numbers_keep_the_stored_double_token():
    workbook = book([])
    workbook.active["A1"] = 12345678901234567890
    assert '"A":1.234567890123457e+19' in read(workbook).blocks[0].text


def test_empty_cells_are_left_out_and_empty_rows_give_no_block():
    workbook = book([["a", None, "c"]])
    sheet = workbook.active
    sheet["A2"], sheet["B2"], sheet["C2"] = "d", "e", "f"
    workbook.active.add_table(Table(displayName="Wide", ref="E1:F3"))
    sheet["E1"], sheet["F1"] = "K", "V"
    sheet["E3"] = "x"
    assert rows(read(workbook)) == [
        {"sheet": "Prices", "row": 3, "table": "Wide", "cells": {"K": "x"}},
        {"sheet": "Prices", "row": 1, "cells": {"A": "a", "C": "c"}},
        {"sheet": "Prices", "row": 2, "cells": {"A": "d", "B": "e", "C": "f"}},
    ]


def test_saved_formula_results_are_read_by_default():
    data = with_saved_results(save(book([[5, 7, "=A1+B1"]])), {"C1": "12"})
    result = read(data)
    assert result.outcome == "success" and not result.findings
    assert rows(result)[0]["cells"] == {"A": 5, "B": 7, "C": 12}


def test_formula_text_is_read_on_request():
    workbook = book([[5, 7, "=A1+B1"]])
    workbook.active["D1"] = ArrayFormula("D1:D1", "=SUM(A1:B1)")
    result = read(workbook, cell_values="formulas")
    assert result.outcome == "success" and not result.findings
    assert rows(result)[0]["cells"] == {
        "A": 5,
        "B": 7,
        "C": "=A1+B1",
        "D": "=SUM(A1:B1)",
    }


def test_missing_saved_results_are_null_and_reported():
    result = read(book([[5, "=A1*2", None, "=A1*3"]]))
    assert result.outcome == "success"
    # The empty C1 makes D1 its own cell group, so row 1 gives two blocks.
    assert [row["cells"] for row in rows(result)] == [{"A": 5, "B": None}, {"D": None}]
    assert [(f.code, f.page_index, f.message) for f in result.findings] == [
        (
            "formula_result_missing",
            0,
            "2 formula cells have no saved result; written as null rather than "
            "calculated.",
        )
    ]


def test_a_row_of_only_missing_results_is_still_emitted():
    result = read(book([["=1+1"]]))
    assert rows(result) == [{"sheet": "Prices", "row": 1, "cells": {"A": None}}]


def test_merged_cells_appear_once_at_their_top_left_address():
    workbook = book([])
    sheet = workbook.active
    sheet.merge_cells("B2:D2")
    sheet["B2"] = "Title"
    assert rows(read(workbook)) == [
        {"sheet": "Prices", "row": 2, "cells": {"B": "Title"}}
    ]


def test_grouping_page_index_and_notes_match_the_text_reader():
    workbook = table_book()
    sheet = workbook.active
    sheet["F8"] = "Note"
    sheet.row_dimensions[3].hidden = True
    buffer = BytesIO()
    from PIL import Image as PilImage

    PilImage.new("RGB", (4, 4)).save(buffer, "PNG")
    buffer.seek(0)
    sheet.add_image(SheetImage(buffer), "H2")
    workbook.create_sheet("Second")["A1"] = "Two"
    data = save(workbook)
    as_json = XlsxJsonTextReader().read(data)
    as_text = XlsxTextReader().read(data)
    assert [b.page_index for b in as_json.blocks] == [0, 0, 0, 1]
    assert [c.replace("xlsx_json", "xlsx") for c in codes(as_json)] == codes(as_text)


def test_no_cell_text_succeeds_with_an_explanation():
    result = read(Workbook())
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


def test_invalid_source_and_configuration():
    result = XlsxJsonTextReader().read(b"private text")
    assert result.outcome == "failed"
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()
    with pytest.raises(ValueError):
        XlsxJsonTextReader(cell_values="values")
    with pytest.raises(TypeError, match="source must"):
        XlsxJsonTextReader().read(BytesIO(save(Workbook())))


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
    result = XlsxJsonTextReader().read(data, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert {block.region_id for block in result.blocks} == {layout.regions[0].id}
    assert [json.loads(b.text)["row"] for b in result.blocks] == [2, 3]


def test_a_range_covering_part_of_a_table_still_uses_its_names():
    result = XlsxJsonTextReader().read(save(table_book()), layout=sheet_layout("B1:B3"))
    assert [json.loads(b.text) for b in result.blocks] == [
        {"sheet": "Prices", "row": 2, "table": "Parts", "cells": {"Price": 120.5}},
        {"sheet": "Prices", "row": 3, "table": "Parts", "cells": {"Price": 45}},
    ]


def test_a_row_mixing_table_and_outside_cells_uses_letters():
    workbook = table_book()
    workbook.active["E2"] = "outside"
    result = XlsxJsonTextReader().read(save(workbook), layout=sheet_layout("A2:E2"))
    assert [json.loads(b.text) for b in result.blocks] == [
        {
            "sheet": "Prices",
            "row": 2,
            "cells": {"A": "Motor", "B": 120.5, "C": "2026-01-02", "E": "outside"},
        }
    ]


def test_inner_ranges_own_their_cells_and_empty_ranges_are_reported():
    data = save(book([["a", "b"], ["c", "d"]]))
    result = XlsxJsonTextReader().read(data, layout=sheet_layout("A1:B2", "B2", "Z9"))
    assert [(b.region_id, json.loads(b.text)["cells"]) for b in result.blocks] == [
        ("r1", {"A": "a", "B": "b"}),
        ("r1", {"A": "c"}),
        ("r2", {"B": "d"}),
    ]
    assert [(f.code, f.region_id) for f in result.findings] == [("empty", "r3")]


def test_missing_results_are_reported_per_range():
    data = save(book([[5, "=A1*2"]]))
    result = XlsxJsonTextReader().read(data, layout=sheet_layout("A1:B1"))
    assert json.loads(result.blocks[0].text)["cells"] == {"A": 5, "B": None}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("formula_result_missing", "r1")
    ]


def test_partly_overlapping_ranges_are_both_rejected():
    data = save(book([["a", "b"], ["c", "d"]]))
    result = XlsxJsonTextReader().read(data, layout=sheet_layout("A1:B1", "B1:B2"))
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["selection_overlap"] * 2


def test_empty_selection_reads_nothing_and_says_so(tmp_path):
    result = XlsxJsonTextReader().read(
        tmp_path / "missing.xlsx", layout=sheet_layout("A1"), region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_png_fixture_dependency_is_available():
    # Guards the image fixture above against a missing Pillow install.
    assert Image.new("RGB", (1, 1)).size == (1, 1)
