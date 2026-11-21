"""pandas-backed CSV and Excel readers: labelled rows, exact values, safe options."""

import builtins
import datetime
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

from raggae.documents.layout_analysis.region_discovery.xlsx import XlsxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    CsvTextReader,
    PandasCsvTextReader,
    PandasExcelTextReader,
    TextReader,
    TextReadResult,
    XlsxTextReader,
)


@pytest.fixture(autouse=True)
def backend():
    return pytest.importorskip("pandas", reason="Install raggae[text-pandas]")


def texts(result):
    return [block.text for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def csv(data, **options):
    return PandasCsvTextReader(options=options or None).read(data)


def workbook_bytes(build):
    workbook = Workbook()
    build(workbook)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def prices(workbook):
    sheet = workbook.active
    sheet.title = "Prices"
    sheet.append(["Part", "Price", "Since", "Ok", "Code"])
    sheet.append(["Motor", 120.5, datetime.datetime(2026, 1, 2), True, "00123"])
    sheet.append(["Gears", 45, datetime.datetime(2026, 3, 4, 9, 30), False, None])
    notes = workbook.create_sheet("Notes")
    notes["A1"], notes["A2"] = "Note", "VAT excluded"


def test_family_exports():
    from raggae.documents.text_reader import csv as csv_readers
    from raggae.documents.text_reader import xlsx as xlsx_readers

    assert csv_readers.PandasCsvTextReader is PandasCsvTextReader
    assert xlsx_readers.PandasExcelTextReader is PandasExcelTextReader


# CSV -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_csv_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = "item,quantity\r\nShipping labels,600\r\nCafé İstanbul,1\r\n".encode()
    path = tmp_path / "orders.csv"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = PandasCsvTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pandas_csv"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == [
        "item: Shipping labels\nquantity: 600",
        "item: Café İstanbul\nquantity: 1",
    ]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.content_format == "plain_text" and block.method == "native"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_csv_output_matches_csv_text_reader_for_ordinary_files():
    data = b'account,note\n00123,"line one\nline two"\n00456,\n'
    assert texts(csv(data)) == texts(CsvTextReader().read(data))


@pytest.mark.parametrize(
    "value", ["00123", "1e5", "TRUE", "NA", "null", "NaN", "N/A", "", " padded "]
)
def test_csv_values_stay_exact_strings_and_are_never_missing_values(value):
    escaped = '"' + value + '"'
    assert texts(csv(f"v,w\n{escaped},x\n".encode())) == [f"v: {value}\nw: x"]


def test_csv_short_rows_are_padded_with_empty_values():
    result = csv(b"a,b,c\n1\n")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["a: 1\nb: \nc: "]


@pytest.mark.parametrize("on_bad_lines", [None, "error", "warn", "skip"])
def test_csv_extra_fields_are_never_silently_lost(on_bad_lines):
    options = {} if on_bad_lines is None else {"on_bad_lines": on_bad_lines}
    result = csv(b"a,b\n1,2,3\n4,5\n", **options)
    # No hidden index column: the first value stays with the first label.
    assert texts(result) == ["a: 1\nb: 2", "a: 4\nb: 5"]
    assert result.outcome == "partial"
    assert codes(result) == ["fields_dropped"]


def test_csv_pandas_header_renaming_is_kept():
    assert texts(csv(b"amount,amount,,c\n1,2,3,4\n")) == [
        "amount: 1\namount.1: 2\nUnnamed: 2: 3\nc: 4"
    ]


def test_csv_without_header_uses_column_labels_and_names_are_honoured():
    assert texts(csv(b"x,y\n", header=None)) == ["Column 1: x\nColumn 2: y"]
    assert texts(csv(b"x,y\n", header=None, names=["first", "second"])) == [
        "first: x\nsecond: y"
    ]


def test_csv_allow_listed_options_work():
    data = b"# exported\nskip me\nitem;qty;note\nTape;12;fragile\nGlue;3;\n"
    # pandas counts comment lines in skiprows: skip "# exported" and "skip me".
    result = csv(
        data, sep=";", comment="#", skiprows=2, usecols=["item", "qty"], nrows=1
    )
    assert texts(result) == ["item: Tape\nqty: 12"]


def test_csv_delimiter_detection_is_opt_in():
    assert texts(csv(b"a;b\n1;2\n")) == ["a;b: 1;2"]
    assert texts(csv(b"a;b\n1;2\n", sep=None, engine="python")) == ["a: 1\nb: 2"]


@pytest.mark.parametrize(
    "data,message",
    [
        (b"", "Source contains no CSV rows."),
        (b"a,b\n", "Source contains a header but no data rows."),
    ],
)
def test_csv_empty_sources_succeed_with_an_explanation(data, message):
    result = csv(data)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]
    assert result.findings[0].message == message


def test_csv_parse_errors_fail_safely():
    result = csv(b'a,b\n"private,unterminated\n', engine="c")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["parse_failed"]
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1252"])
def test_csv_declared_encodings(encoding):
    data = "name\nCafé\n".encode(encoding)
    assert texts(PandasCsvTextReader(encoding=encoding).read(data)) == ["name: Café"]


def test_csv_decode_and_source_failures(tmp_path, monkeypatch):
    assert codes(csv(b"name\nprivate\xff\n")) == ["decode_failed"]
    reader = PandasCsvTextReader()
    assert codes(reader.read(tmp_path / "missing.csv")) == ["source_unreadable"]
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


# Options -------------------------------------------------------------------


@pytest.mark.parametrize(
    "reader,options,error",
    [
        (PandasCsvTextReader, {"dtype": float}, ValueError),
        (PandasCsvTextReader, {"converters": {}}, ValueError),
        (PandasCsvTextReader, {"na_values": ["x"]}, ValueError),
        (PandasCsvTextReader, {"keep_default_na": True}, ValueError),
        (PandasCsvTextReader, {"index_col": 0}, ValueError),
        (PandasCsvTextReader, {"parse_dates": True}, ValueError),
        (PandasCsvTextReader, {"chunksize": 10}, ValueError),
        (PandasCsvTextReader, {"storage_options": {}}, ValueError),
        (PandasCsvTextReader, {"encoding": "latin-1"}, ValueError),
        (PandasCsvTextReader, {"on_bad_lines": print}, ValueError),
        (PandasCsvTextReader, {"engine": "pyarrow"}, ValueError),
        (PandasCsvTextReader, ["sep"], TypeError),
        (PandasCsvTextReader, {1: ","}, TypeError),
        (PandasExcelTextReader, {"dtype": str}, ValueError),
        (PandasExcelTextReader, {"engine": "xlrd"}, ValueError),
        (PandasExcelTextReader, {"converters": {}}, ValueError),
    ],
)
def test_options_outside_the_allow_list_are_refused(reader, options, error):
    with pytest.raises(error):
        reader(options=options)


def test_refusal_names_the_option():
    with pytest.raises(ValueError, match="'dtype' is not allowed"):
        PandasCsvTextReader(options={"dtype": float})


def test_options_are_copied_at_construction():
    options = {"sep": ";"}
    reader = PandasCsvTextReader(options=options)
    options["sep"] = ","
    assert texts(reader.read(b"a;b\n1;2\n")) == ["a: 1\nb: 2"]


# Excel -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_excel_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = workbook_bytes(prices)
    path = tmp_path / "prices.xlsx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = PandasExcelTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pandas_excel"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (0, "Part: Motor\nPrice: 120.5\nSince: 2026-01-02\nOk: TRUE\nCode: 00123"),
        (0, "Part: Gears\nPrice: 45\nSince: 2026-03-04T09:30:00\nOk: FALSE\nCode: "),
        (1, "Note: VAT excluded"),
    ]
    assert all(b.content_format == "plain_text" for b in result.blocks)
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_excel_values_match_xlsx_text_reader_formatting():
    data = workbook_bytes(prices)
    grid = XlsxTextReader().read(data).blocks[0].text.splitlines()
    first = PandasExcelTextReader().read(data).blocks[0].text
    assert [line.split(": ", 1)[1] for line in first.splitlines()] == grid[1].split(
        " | "
    )


def test_excel_sheet_and_other_options():
    data = workbook_bytes(prices)
    by_name = PandasExcelTextReader(options={"sheet_name": "Notes"}).read(data)
    assert [(b.page_index, b.text) for b in by_name.blocks] == [
        (1, "Note: VAT excluded")
    ]
    by_position = PandasExcelTextReader(options={"sheet_name": 1}).read(data)
    assert texts(by_position) == texts(by_name)
    narrow = PandasExcelTextReader(
        options={"sheet_name": "Prices", "usecols": "A:B", "nrows": 1}
    )
    assert texts(narrow.read(data)) == ["Part: Motor\nPrice: 120.5"]
    positional = PandasExcelTextReader(options={"sheet_name": "Notes", "header": None})
    assert texts(positional.read(data)) == ["Column 1: Note", "Column 1: VAT excluded"]


def test_excel_missing_formula_results_are_reported():
    def build(workbook):
        sheet = workbook.active
        sheet.append(["Price", "Total"])
        sheet.append([5, "=A2*2"])
        sheet.append([7, "=A3*2"])

    result = PandasExcelTextReader().read(workbook_bytes(build))
    assert result.outcome == "success"
    assert texts(result) == ["Price: 5\nTotal: ", "Price: 7\nTotal: "]
    assert [(f.code, f.page_index, f.message) for f in result.findings] == [
        (
            "formula_result_missing",
            0,
            "2 formula cells have no saved result on this sheet; left empty rather "
            "than calculated.",
        )
    ]


def test_excel_chart_sheets_are_reported_not_read():
    def build(workbook):
        prices(workbook)
        chart = BarChart()
        chart.add_data(Reference(workbook["Prices"], min_col=2, min_row=1, max_row=3))
        workbook.create_chartsheet("Chart").add_chart(chart)

    result = PandasExcelTextReader().read(workbook_bytes(build))
    assert result.outcome == "success"
    assert len(result.blocks) == 3
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("unsupported_content", 2)
    ]


def test_excel_empty_workbook_and_unknown_sheet():
    empty = PandasExcelTextReader().read(workbook_bytes(lambda workbook: None))
    assert empty.outcome == "success" and codes(empty) == ["empty"]
    unknown = PandasExcelTextReader(options={"sheet_name": "private-missing"}).read(
        workbook_bytes(prices)
    )
    assert unknown.outcome == "failed"
    assert codes(unknown) == ["parse_failed"]
    assert "private" not in unknown.model_dump_json()


@pytest.mark.parametrize("data", [b"", b"private text", b"PK\x03\x04broken"])
def test_excel_invalid_sources_fail_safely(data):
    result = PandasExcelTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


# Regions and backend -------------------------------------------------------


def test_layout_regions_are_rejected_before_source_io(tmp_path, monkeypatch):
    data = workbook_bytes(prices)
    layout = XlsxLayoutAnalyzer().analyze(data)
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    for reader in (PandasExcelTextReader(), PandasCsvTextReader()):
        result = reader.read(tmp_path / "missing", layout=layout)
        assert result.outcome == "failed" and not result.blocks
        assert {f.code for f in result.findings} == {"selection_unsupported"}
        assert [f.region_id for f in result.findings] == [r.id for r in layout.regions]
        empty = reader.read(tmp_path / "missing", layout=layout, region_ids=[])
        assert empty.outcome == "failed" and not empty.blocks
        assert [(f.code, f.region_id) for f in empty.findings] == [
            ("selection_unsupported", None)
        ]
    assert (
        "XlsxTextReader"
        in PandasExcelTextReader()
        .read(tmp_path / "missing", layout=layout)
        .findings[0]
        .message
    )


def test_missing_backend_has_an_install_hint(monkeypatch):
    data = workbook_bytes(prices)
    real_import = builtins.__import__

    def without_pandas(name, *args, **kwargs):
        if name == "pandas" or name.startswith("pandas."):
            raise ImportError("no pandas")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pandas)
    with pytest.raises(ImportError, match=r"raggae\[text-pandas\]"):
        PandasCsvTextReader().read(b"a\n1\n")
    with pytest.raises(ImportError, match=r"raggae\[text-pandas\]"):
        PandasExcelTextReader().read(data)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        PandasCsvTextReader().read(BytesIO(b"a\n1\n"))
    with pytest.raises(TypeError, match="source must"):
        PandasExcelTextReader().read(BytesIO(workbook_bytes(prices)))
