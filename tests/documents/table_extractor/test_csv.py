"""The table a CSV file is."""

import json

import pytest

from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    CsvTableExtractor,
    HtmlTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from raggae.documents.text_reader import CsvJsonTextReader
from tests.documents.image_extractor.conftest import make_layout


def extract(text, **options):
    data = text.encode() if isinstance(text, str) else text
    return CsvTableExtractor(**options).extract(data)


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def rows(table):
    found = {}
    for cell in table.cells:
        found.setdefault(cell.row, []).append(cell.text)
    return [found[row] for row in sorted(found)]


PARTS = 'Part,Price,Note\r\nLamp head,110,"two\nlines"\r\n\r\nStand,00045.50,\r\n'


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import csv

    assert csv.CsvTableExtractor is CsvTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = PARTS.encode()
    path = tmp_path / "parts.csv"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = CsvTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_csv"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    # Strings as written: never trimmed or typed; a quoted line break kept; blank
    # lines skipped; an empty field is an empty cell.
    assert rows(table) == [
        ["Part", "Price", "Note"],
        ["Lamp head", "110", "two\nlines"],
        ["Stand", "00045.50", ""],
    ]
    assert [c.header for c in table.cells] == [True] * 3 + [False] * 6
    assert (table.row_count, table.column_count, table.page_index) == (3, 3, 0)
    assert (table.method, table.region_id, table.caption) == ("declared", None, None)
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_the_json_form_is_what_the_csv_json_reader_writes():
    data = b"item,quantity\nTape,12\nGlue,00123\n"
    (table,) = CsvTableExtractor().extract(data).tables
    records = CsvJsonTextReader().read(data)
    assert json.loads(table.to_json()) == [json.loads(b.text) for b in records.blocks]


def test_the_html_form_reads_back_into_the_same_cells():
    (table,) = extract(PARTS).tables
    (again,) = HtmlTableExtractor().extract(table.to_html().encode()).tables
    assert again.cells == table.cells


def test_without_a_header_no_cell_is_a_header():
    (table,) = extract("a,b\nc,d\n", header=False).tables
    assert not any(c.header for c in table.cells)
    assert table.to_markdown().splitlines()[0] == "| Column 1 | Column 2 |"


def test_ragged_rows_keep_the_fields_written():
    result = extract("A,B,C\nshort\none,two,three,four\nx,y,z\n")
    (table,) = result.tables
    assert [(c.row, c.column) for c in table.cells if c.row == 1] == [(1, 0)]
    assert rows(table)[2] == ["one", "two", "three", "four"]
    assert table.column_count == 4
    # Nothing is missing, so the read succeeds; the first misaligned row is named.
    assert result.outcome == "success" and codes(result) == [("row_length_mismatch", 0)]
    assert result.findings[0].message.startswith(
        "Row starting on line 2 has 1 field; the header has 3 fields."
    )
    headerless = extract("a\nb,c\n", header=False)
    assert headerless.outcome == "success" and not headerless.findings


@pytest.mark.parametrize(("delimiter", "text"), [(";", "a;b\n1;2\n"), ("\t", "a\tb\n1\t2\n")])
def test_the_delimiter_is_configured(delimiter, text):
    (table,) = extract(text, delimiter=delimiter).tables
    assert rows(table) == [["a", "b"], ["1", "2"]]


@pytest.mark.parametrize(
    "options",
    [{"header": 1}, {"delimiter": ";;"}, {"delimiter": '"'}, {"delimiter": 1}, {"encoding": "latin-9"}],
)
def test_options_are_validated(options):
    with pytest.raises((TypeError, ValueError)):
        CsvTableExtractor(**options)


def test_malformed_quoting_ends_the_read_and_earlier_rows_are_kept():
    result = extract('A,B\n1,2\n3,"open\n')
    assert rows(result.tables[0]) == [["A", "B"], ["1", "2"]]
    assert result.outcome == "partial" and codes(result) == [("parse_failed", 0)]
    first = extract('"open\n')
    assert first.outcome == "failed" and not first.tables
    assert codes(first) == [("parse_failed", 0)]


def test_a_field_above_the_csv_size_limit_ends_the_read():
    result = extract("A\nok\n" + "x" * 200_000 + "\n")
    assert rows(result.tables[0]) == [["A"], ["ok"]]
    assert result.outcome == "partial" and codes(result) == [("field_too_large", 0)]


def test_empty_files_and_files_without_text():
    for text in ("", "\n\n"):
        result = extract(text)
        assert result.outcome == "success" and codes(result) == [("empty", 0)]
    result = extract(",,\n,,\n")
    assert not result.tables and codes(result) == [("empty_table", 0)]


def test_a_header_only_file_is_a_table_of_one_row():
    (table,) = extract("A,B\n").tables
    assert rows(table) == [["A", "B"]] and table.to_json() == "[]"


def test_encoding_is_configured_never_guessed(tmp_path):
    data = "Café,Prix\n".encode("cp1252")
    assert extract(data, encoding="cp1252").tables[0].cells[0].text == "Café"
    failed = extract(data)
    assert failed.outcome == "failed" and codes(failed) == [("decode_failed", None)]
    assert codes(extract(tmp_path / "missing.csv")) == [("source_unreadable", None)]


def test_layouts_are_refused_before_the_source_is_opened(tmp_path):
    missing = tmp_path / "missing.csv"
    layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = CsvTableExtractor().extract(missing, layout=layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "grid")
    ]
