"""CSV rows become JSON objects (or arrays without a header) of exact strings."""

import csv
import json
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    CsvJsonTextReader,
    TextReader,
    TextReadResult,
)


@pytest.fixture
def layout():
    return LayoutAnalysisResult(
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
                id="first", surface_id="page-1", kind="text", bbox=(0, 0, 20, 20)
            ),
            LayoutRegion(
                id="second", surface_id="page-1", kind="text", bbox=(0, 30, 20, 50)
            ),
        ),
    )


def values(result):
    return [json.loads(block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def test_csv_family_exports_the_same_worker():
    from raggae.documents.text_reader import csv as csv_readers

    assert csv_readers.CsvJsonTextReader is CsvJsonTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = (
        "item,quantity,amount\r\nShipping labels,600,90\r\nCafé İstanbul,1,5\r\n"
    ).encode()
    path = tmp_path / "orders.csv"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = CsvJsonTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_csv_json"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert values(result) == [
        {"item": "Shipping labels", "quantity": "600", "amount": "90"},
        {"item": "Café İstanbul", "quantity": "1", "amount": "5"},
    ]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.method == "native" and block.content_format == "json"
        assert set(block.model_dump()) == {
            "text",
            "page_index",
            "region_id",
            "method",
            "content_format",
        }
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()  # No handles remain open after reading.


def test_output_is_compact_and_keeps_column_order():
    result = CsvJsonTextReader().read(b"z,a,m\n1,2,3\n")
    assert result.blocks[0].text == '{"z":"1","a":"2","m":"3"}'


@pytest.mark.parametrize(
    "value",
    ["00123", "1e5", "-0", "1,000.50", "TRUE", "null", "NaN", "", " padded "],
)
def test_values_stay_exact_json_strings(value):
    escaped = '"' + value.replace('"', '""') + '"'
    result = CsvJsonTextReader().read(f"account\n{escaped}\n".encode())
    assert values(result) == [{"account": value}]
    assert isinstance(values(result)[0]["account"], str)


def test_quoted_newlines_quotes_and_control_characters_round_trip():
    source = b'id,note\n1,"line one\r\nline two, ""quoted""\ttab\x00nul"\n'
    result = CsvJsonTextReader().read(source)
    assert values(result) == [
        {"id": "1", "note": 'line one\r\nline two, "quoted"\ttab\x00nul'}
    ]


def test_empty_fields_are_empty_strings():
    result = CsvJsonTextReader().read(b"a,b,c\n,,\n1,,3\n")
    assert values(result) == [
        {"a": "", "b": "", "c": ""},
        {"a": "1", "b": "", "c": "3"},
    ]


def test_header_names_are_kept_verbatim_as_keys():
    result = CsvJsonTextReader().read(" Name ,Ünvan,a.b/c\nAda,Dr,x\n".encode())
    assert values(result) == [{" Name ": "Ada", "Ünvan": "Dr", "a.b/c": "x"}]


def test_duplicate_header_names_are_renamed_with_numbered_suffixes():
    result = CsvJsonTextReader().read(b"amount,amount,note,amount\n90,10,x,5\n")
    assert result.outcome == "success"
    assert result.blocks[0].text == (
        '{"amount":"90","amount_2":"10","note":"x","amount_3":"5"}'
    )
    assert codes(result) == ["header_renamed"] * 2
    assert [f.message for f in result.findings] == [
        "Header column 2 repeats an earlier column name; its key was given a "
        "numbered suffix.",
        "Header column 4 repeats an earlier column name; its key was given a "
        "numbered suffix.",
    ]
    assert all(f.page_index == 0 for f in result.findings)
    assert "amount" not in result.findings[0].message


def test_renaming_never_collides_with_a_real_column_name():
    result = CsvJsonTextReader().read(b"amount,amount,amount_2\n1,2,3\n")
    assert result.blocks[0].text == '{"amount":"1","amount_3":"2","amount_2":"3"}'
    assert codes(result) == ["header_renamed"]


def test_duplicate_names_are_case_sensitive():
    result = CsvJsonTextReader().read(b"Amount,amount\n1,2\n")
    assert values(result) == [{"Amount": "1", "amount": "2"}]
    assert not result.findings


def test_renamed_header_still_reported_when_no_data_rows():
    result = CsvJsonTextReader().read(b"a,a\n")
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == [
        "header_renamed",
        "empty",
    ]


@pytest.mark.parametrize("header", [b"name,,city", b"name,   ,city", b",name"])
def test_blank_header_name_fails_the_read(header):
    result = CsvJsonTextReader().read(header + b"\nAda,x,London\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["header_blank"]
    column = 1 if header.startswith(b",") else 2
    assert result.findings[0].message == (
        f"Header column {column} has no name; no rows were returned."
    )
    assert result.findings[0].page_index == 0


def test_blank_header_fails_before_reading_later_malformed_rows():
    result = CsvJsonTextReader().read(b'a,\n1,2\n"open\n')
    assert codes(result) == ["header_blank"]


def test_headerless_rows_are_arrays_and_may_vary_in_length():
    result = CsvJsonTextReader(header=False).read(b"00123,Ada\n7\n,\n")
    assert result.outcome == "success" and not result.findings
    assert [block.text for block in result.blocks] == [
        '["00123","Ada"]',
        '["7"]',
        '["",""]',
    ]


def test_headerless_blank_and_duplicate_first_row_is_just_data():
    result = CsvJsonTextReader(header=False).read(b"a,a,\n")
    assert values(result) == [["a", "a", ""]]
    assert not result.findings


@pytest.mark.parametrize(
    "bad_row,count",
    [(b"4,5,6,7", "4 fields"), (b"4", "1 field"), (b"4,5", "2 fields")],
)
def test_row_length_mismatch_keeps_earlier_rows_and_stops(bad_row, count):
    source = b"a,b,c\n1,2,3\n\n" + bad_row + b"\n7,8,9\n"
    result = CsvJsonTextReader().read(source)
    assert result.outcome == "partial"
    assert values(result) == [{"a": "1", "b": "2", "c": "3"}]
    assert codes(result) == ["row_length_mismatch"]
    assert result.findings[0].message == (
        f"Row starting on line 4 has {count}; the header has 3 fields; "
        "it and later rows were not returned."
    )


def test_row_length_mismatch_without_completed_rows_fails():
    result = CsvJsonTextReader().read(b"a,b\n1\n2,3\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["row_length_mismatch"]


def test_rename_findings_are_kept_when_a_later_row_stops_the_read():
    result = CsvJsonTextReader().read(b"a,a\n1,2\n3\n")
    assert result.outcome == "partial"
    assert values(result) == [{"a": "1", "a_2": "2"}]
    assert codes(result) == [
        "header_renamed",
        "row_length_mismatch",
    ]


def test_malformed_row_keeps_earlier_rows_and_stops():
    result = CsvJsonTextReader().read(b'a,b\n1,2\n3,"x"y\n5,6\n')
    assert result.outcome == "partial"
    assert values(result) == [{"a": "1", "b": "2"}]
    assert codes(result) == ["parse_failed"]
    assert "line 3" in result.findings[0].message


@pytest.mark.parametrize("source", [b'"a"b\n1\n', b'a\n"x"y\n', b'a\n\n"open'])
def test_malformed_without_completed_rows_fails(source):
    result = CsvJsonTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["parse_failed"]


def test_stdlib_field_limit_is_reported_not_changed():
    limit = csv.field_size_limit()
    source = b"a\n1\n" + b"x" * (limit + 1) + b"\n2\n"
    result = CsvJsonTextReader().read(source)
    assert result.outcome == "partial" and values(result) == [{"a": "1"}]
    assert codes(result) == ["field_too_large"]
    assert csv.field_size_limit() == limit


@pytest.mark.parametrize(
    "source,header,message",
    [
        (b"", True, "Source contains no CSV rows."),
        (b"\n\n", True, "Source contains no CSV rows."),
        (b"a,b\n", True, "Source contains a header but no data rows."),
        (b"", False, "Source contains no CSV rows."),
    ],
)
def test_empty_sources_succeed_with_an_explanation(source, header, message):
    result = CsvJsonTextReader(header=header).read(source)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]
    assert result.findings[0].message == message


def test_blank_lines_are_skipped_including_before_the_header():
    result = CsvJsonTextReader().read(b"\n\r\nname\n\nAda\n\n")
    assert values(result) == [{"name": "Ada"}]


@pytest.mark.parametrize("delimiter", ["\t", ";", "|"])
def test_configured_delimiter(delimiter):
    source = f"a{delimiter}b\n1,5{delimiter}x\n".encode()
    result = CsvJsonTextReader(delimiter=delimiter).read(source)
    assert values(result) == [{"a": "1,5", "b": "x"}]


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"],
)
def test_declared_encodings(encoding):
    name = "Cafe" if encoding == "ascii" else "Café"
    source = f"name,id\n{name},00123\n".encode(encoding)
    result = CsvJsonTextReader(encoding=encoding).read(source)
    assert result.outcome == "success" and not result.findings
    assert values(result) == [{"name": name, "id": "00123"}]


def test_default_utf8_keeps_a_bom_in_the_first_key():
    result = CsvJsonTextReader().read(b"\xef\xbb\xbfname\nAda\n")
    assert values(result) == [{"﻿name": "Ada"}]


def test_decode_failure_is_source_wide_and_private():
    result = CsvJsonTextReader().read(b"name\nprivate\xff\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = CsvJsonTextReader()
    result = reader.read(tmp_path / "private-name.csv")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_layout_rejection_precedes_io(tmp_path, monkeypatch, layout, selection):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = CsvJsonTextReader().read(
        tmp_path / "missing.csv", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert set(codes(result)) == {"selection_unsupported"}
    assert result.findings[0].message == (
        "CsvJsonTextReader reads whole CSV sources, not layout regions."
    )


def test_a_layout_is_refused_even_when_nothing_is_selected(tmp_path, layout):
    result = CsvJsonTextReader().read(
        tmp_path / "does-not-exist", layout=layout, region_ids=[]
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"encoding": None}, TypeError),
        ({"encoding": "unknown-codec"}, ValueError),
        ({"header": 1}, TypeError),
        ({"delimiter": ""}, ValueError),
        ({"delimiter": '"'}, ValueError),
        ({"delimiter": "\n"}, ValueError),
    ],
)
def test_configuration_errors(kwargs, error):
    with pytest.raises(error):
        CsvJsonTextReader(**kwargs)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        CsvJsonTextReader().read(BytesIO(b"a\n1\n"))
