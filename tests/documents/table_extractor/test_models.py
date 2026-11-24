"""The table contract: cells, findings, results, and the five forms of a table."""

import json
import sys
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from raggae.documents.table_extractor import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from raggae.documents.text_reader import FindingCode


def cell(row, column, text, **options):
    return TableCell(row=row, column=column, text=text, **options)


def table(*cells, rows, columns, **options):
    return ExtractedTable(
        cells=cells, row_count=rows, column_count=columns, method="declared", **options
    )


# "Part" covers two header rows; "Price" covers two columns.
PRICES = table(
    cell(0, 0, "Part", row_span=2, header=True),
    cell(0, 1, "Price", column_span=2, header=True),
    cell(1, 1, "2025", header=True),
    cell(1, 2, "2026", header=True),
    cell(2, 0, "Motor"),
    cell(2, 1, "110"),
    cell(2, 2, "120"),
    rows=3,
    columns=3,
    caption="Prices",
)

# "North" covers two data rows.
REGIONS = table(
    cell(0, 0, "Region", header=True),
    cell(0, 1, "Year", header=True),
    cell(0, 2, "Sales", header=True),
    cell(1, 0, "North", row_span=2),
    cell(1, 1, "2025"),
    cell(1, 2, "10"),
    cell(2, 1, "2026"),
    cell(2, 2, "12"),
    rows=3,
    columns=3,
)


# Contract ----------------------------------------------------------------------------


def test_shared_codes_keep_the_text_reader_values():
    shared = [code for code in TableFindingCode if code.name in FindingCode.__members__]
    assert all(FindingCode[code.name].value == code.value for code in shared)
    assert {code.name for code in TableFindingCode} - {code.name for code in shared} == {
        "CELLS_OUTSIDE_TABLES",
        "EMPTY_TABLE",
        "NESTED_TABLE",
        "SPAN_SHORTENED",
    }


@pytest.mark.parametrize(
    "options",
    [
        {"row": -1},
        {"column": "1"},
        {"row_span": 0},
        {"column_span": 0},
        {"header": 1},
        {"style": "bold"},
    ],
)
def test_cells_reject_invalid_values(options):
    with pytest.raises(ValidationError):
        TableCell(**{"row": 0, "column": 0, "text": "x", **options})


@pytest.mark.parametrize(
    ("cells", "rows", "columns"),
    [
        ((cell(0, 1, "b"), cell(0, 0, "a")), 1, 2),  # not row by row
        ((cell(0, 0, "a"), cell(0, 0, "b")), 1, 1),  # one position twice
        ((cell(0, 0, "a", column_span=2),), 1, 1),  # outside the grid
        ((cell(0, 0, "a", row_span=2),), 1, 1),
        ((cell(0, 0, "a", row_span=2), cell(1, 0, "b")), 2, 1),  # overlap
        ((cell(0, 0, "a", column_span=2), cell(0, 1, "b")), 1, 2),
        ((cell(0, 0, ""), cell(0, 1, " \n")), 1, 2),  # no text
        ((), 1, 1),
    ],
)
def test_tables_reject_invalid_grids(cells, rows, columns):
    with pytest.raises(ValidationError):
        table(*cells, rows=rows, columns=columns)


def test_tables_require_a_method_and_a_nonempty_caption():
    with pytest.raises(ValidationError):
        ExtractedTable(cells=(cell(0, 0, "a"),), row_count=1, column_count=1)
    with pytest.raises(ValidationError):
        table(cell(0, 0, "a"), rows=1, columns=1, caption="")
    with pytest.raises(ValidationError):
        ExtractedTable(
            cells=(cell(0, 0, "a"),), row_count=1, column_count=1, method="guessed"
        )


def test_short_rows_and_empty_rows_are_valid():
    short = table(
        cell(0, 0, "a"), cell(0, 1, "b"), cell(1, 0, "c"), cell(3, 1, "d"), rows=4, columns=2
    )
    assert short.row_count == 4 and len(short.cells) == 4


def test_results_are_consistent_and_round_trip_through_json():
    with pytest.raises(ValidationError):
        TableExtractResult(
            extractor="x",
            outcome="failed",
            tables=(PRICES,),
            findings=(TableExtractFinding(code="empty", message="m"),),
        )
    with pytest.raises(ValidationError):
        TableExtractResult(extractor="x", outcome="partial")
    result = TableExtractResult(
        extractor="table_extractor_html",
        outcome="success",
        tables=(PRICES, REGIONS),
        findings=(TableExtractFinding(code="nested_table", message="m", page_index=0),),
    )
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        PRICES.row_count = 4


# The five forms -------------------------------------------------------------------------


def test_html_keeps_spans_header_cells_and_the_caption():
    assert PRICES.to_html() == (
        "<table>\n"
        "<caption>Prices</caption>\n"
        '<tr><th rowspan="2">Part</th><th colspan="2">Price</th></tr>\n'
        "<tr><th>2025</th><th>2026</th></tr>\n"
        "<tr><td>Motor</td><td>110</td><td>120</td></tr>\n"
        "</table>"
    )


def test_row_based_forms_of_a_table_with_two_header_rows():
    assert PRICES.to_markdown() == (
        "| Part | Price / 2025 | Price / 2026 |\n|---|---|---|\n| Motor | 110 | 120 |"
    )
    assert json.loads(PRICES.to_json()) == [
        {"Part": "Motor", "Price / 2025": "110", "Price / 2026": "120"}
    ]
    assert PRICES.to_json() == (
        '[{"Part":"Motor","Price / 2025":"110","Price / 2026":"120"}]'
    )
    assert PRICES.to_xml() == (
        "<table>\n"
        "  <caption>Prices</caption>\n"
        '  <row><cell column="Part">Motor</cell><cell column="Price / 2025">110</cell>'
        '<cell column="Price / 2026">120</cell></row>\n'
        "</table>"
    )
    frame = PRICES.to_dataframe()
    assert list(frame.columns) == ["Part", "Price / 2025", "Price / 2026"]
    assert frame.to_dict("records") == [
        {"Part": "Motor", "Price / 2025": "110", "Price / 2026": "120"}
    ]
    assert all(dtype is object or dtype.kind == "O" for dtype in frame.dtypes)


def test_a_value_spanning_data_rows_appears_once():
    assert json.loads(REGIONS.to_json()) == [
        {"Region": "North", "Year": "2025", "Sales": "10"},
        {"Region": None, "Year": "2026", "Sales": "12"},
    ]
    assert REGIONS.to_markdown().splitlines()[2:] == [
        "| North | 2025 | 10 |",
        "|  | 2026 | 12 |",
    ]
    assert '<row><cell column="Year">2026</cell>' in REGIONS.to_xml()
    frame = REGIONS.to_dataframe()
    assert frame["Region"].tolist() == ["North", None]
    # Only positions where no cell starts are None, so filling them is exactly
    # the repeated form.
    assert frame.ffill()["Region"].tolist() == ["North", "North"]
    assert '<td rowspan="2">North</td>' in REGIONS.to_html()


def test_labels_without_headers_blank_headers_and_repeats():
    headerless = table(cell(0, 0, "a"), cell(0, 1, "b"), rows=1, columns=2)
    assert headerless.to_markdown() == "| Column 1 | Column 2 |\n|---|---|\n| a | b |"
    assert json.loads(headerless.to_json()) == [{"Column 1": "a", "Column 2": "b"}]
    labelled = table(
        cell(0, 0, "Amount", header=True),
        cell(0, 1, "", header=True),
        cell(0, 2, "Amount", header=True),
        cell(1, 0, "1"),
        cell(1, 1, "2"),
        cell(1, 2, "3"),
        rows=2,
        columns=3,
    )
    assert list(labelled.to_dataframe().columns) == ["Amount", "Column 2", "Amount_2"]
    # A first row mixing header and data cells is not a header row.
    mixed = table(cell(0, 0, "Name", header=True), cell(0, 1, "Ada"), rows=1, columns=2)
    assert json.loads(mixed.to_json()) == [{"Column 1": "Name", "Column 2": "Ada"}]


def test_a_table_of_headers_only_has_no_data_rows():
    headers = table(cell(0, 0, "A", header=True), cell(0, 1, "B", header=True), rows=1, columns=2)
    assert headers.to_markdown() == "| A | B |\n|---|---|"
    assert headers.to_json() == "[]"
    assert headers.to_xml() == "<table>\n</table>"
    frame = headers.to_dataframe()
    assert list(frame.columns) == ["A", "B"] and frame.empty


def test_short_and_empty_rows_in_each_form():
    short = table(
        cell(0, 0, "a"), cell(0, 1, "b"), cell(1, 0, "c"), cell(3, 1, "d"), rows=4, columns=2
    )
    assert short.to_html().splitlines()[1:-1] == [
        "<tr><td>a</td><td>b</td></tr>",
        "<tr><td>c</td></tr>",  # a short row is not padded
        "<tr></tr>",
        "<tr><td></td><td>d</td></tr>",  # an uncovered position before a cell
    ]
    assert json.loads(short.to_json())[1:] == [
        {"Column 1": "c", "Column 2": None},
        {"Column 1": None, "Column 2": None},
        {"Column 1": None, "Column 2": "d"},
    ]
    assert short.to_xml().splitlines()[2:4] == [
        '  <row><cell column="Column 1">c</cell></row>',
        "  <row/>",
    ]


def test_text_is_escaped_for_each_form():
    tricky = table(
        cell(0, 0, 'A | "B"', header=True),
        cell(1, 0, "x < y & z\nnext line"),
        rows=2,
        columns=1,
        caption="Tom & Jerry",
    )
    assert tricky.to_markdown() == '| A \\| "B" |\n|---|\n| x < y & z next line |'
    html = tricky.to_html()
    assert "<caption>Tom &amp; Jerry</caption>" in html
    assert "<td>x &lt; y &amp; z<br>next line</td>" in html
    root = ET.fromstring(tricky.to_xml())
    assert root.findtext("caption") == "Tom & Jerry"
    assert root.findtext("row/cell") == "x < y & z\nnext line"
    assert [value.get("column") for value in root.iter("cell")] == ['A | "B"']
    assert json.loads(tricky.to_json()) == [{'A | "B"': "x < y & z\nnext line"}]
    assert tricky.to_dataframe().iloc[0, 0] == "x < y & z\nnext line"


def test_xml_refuses_characters_it_cannot_represent():
    control = table(cell(0, 0, "page\x0bbreak"), rows=1, columns=1)
    with pytest.raises(ValueError, match="XML 1.0"):
        control.to_xml()
    assert control.to_json() == '[{"Column 1":"page\\u000bbreak"}]'


def test_dataframe_needs_pandas(monkeypatch):
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ImportError, match=r"raggae\[text-pandas\]"):
        PRICES.to_dataframe()
