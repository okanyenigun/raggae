"""CSV rows become labelled plain-text blocks without changing any field value."""

import csv
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
    CsvTextReader,
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


def texts(result):
    return [block.text for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def test_csv_family_exports_the_same_worker():
    from raggae.documents.text_reader import csv as csv_readers

    assert csv_readers.CsvTextReader is CsvTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = "item,quantity,amount\r\nShipping labels,600,90\r\nCafé İstanbul,1,5\r\n".encode()
    path = tmp_path / "orders.csv"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = CsvTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_csv"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == [
        "item: Shipping labels\nquantity: 600\namount: 90",
        "item: Café İstanbul\nquantity: 1\namount: 5",
    ]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
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


@pytest.mark.parametrize(
    "value",
    ["00123", "1e5", "-0", "1,000.50", "TRUE", "null", "NaN", "2024-01-02", " padded "],
)
def test_values_stay_exact_strings(value):
    escaped = '"' + value.replace('"', '""') + '"'
    result = CsvTextReader().read(f"account\n{escaped}\n".encode())
    assert texts(result) == [f"account: {value}"]


def test_quoted_multiline_value_stays_in_its_row_with_original_line_endings():
    source = b'id,note\n1,"line one\r\nline two\nline three"\n2,after\n'
    result = CsvTextReader().read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == [
        "id: 1\nnote: line one\r\nline two\nline three",
        "id: 2\nnote: after",
    ]


def test_quoting_escapes_delimiters_quotes_and_unicode_separators():
    source = 'a,b\n"x, y","say ""hi"""\n"p q",r s\n'.encode()
    result = CsvTextReader().read(source)
    assert texts(result) == [
        'a: x, y\nb: say "hi"',
        "a: p q\nb: r s",
    ]


def test_empty_fields_are_kept_with_their_labels():
    result = CsvTextReader().read(b"a,b,c\n,,\n1,,3\n")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["a: \nb: \nc: ", "a: 1\nb: \nc: 3"]


def test_headerless_rows_use_column_labels_and_may_vary_in_length():
    result = CsvTextReader(header=False).read(b"00123,Ada\n7\n")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["Column 1: 00123\nColumn 2: Ada", "Column 1: 7"]


def test_blank_header_cells_get_positional_labels_and_names_stay_verbatim():
    result = CsvTextReader().read(b" Name ,,   ,Note\nAda,x,y,z\n")
    assert texts(result) == [" Name : Ada\nColumn 2: x\nColumn 3: y\nNote: z"]


def test_duplicate_headers_keep_every_value():
    result = CsvTextReader().read(b"amount,amount\n90,10\n")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["amount: 90\namount: 10"]


@pytest.mark.parametrize(
    "bad_row,count",
    [(b"4,5,6,7", "4 fields"), (b"4", "1 field"), (b"4,5", "2 fields")],
)
def test_row_length_mismatch_keeps_earlier_rows_and_stops(bad_row, count):
    source = b"a,b,c\n1,2,3\n\n" + bad_row + b"\n7,8,9\n"
    result = CsvTextReader().read(source)
    assert result.outcome == "partial"
    assert texts(result) == ["a: 1\nb: 2\nc: 3"]
    assert codes(result) == ["row_length_mismatch"]
    assert result.findings[0].message == (
        f"Row starting on line 4 has {count}; the header has 3 fields; "
        "it and later rows were not returned."
    )
    assert result.findings[0].page_index == 0


@pytest.mark.parametrize("source", [b"a,b\n1\n2,3\n", b"a,b\n1,2,3\n"])
def test_row_length_mismatch_without_completed_rows_fails(source):
    result = CsvTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["row_length_mismatch"]


def test_row_length_mismatch_stops_before_a_later_parse_failure():
    result = CsvTextReader().read(b'a,b\n1,2\n3\n"x"y\n')
    assert result.outcome == "partial" and texts(result) == ["a: 1\nb: 2"]
    assert codes(result) == ["row_length_mismatch"]


def test_blank_lines_are_skipped_including_before_the_header():
    result = CsvTextReader().read(b"\n\r\nname\n\nAda\n\n")
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["name: Ada"]


def test_single_empty_quoted_field_is_a_row_not_a_blank_line():
    result = CsvTextReader().read(b'name\n""\n')
    assert texts(result) == ["name: "]


@pytest.mark.parametrize("terminator", ["", "\n", "\r\n", "\r"])
def test_record_terminators_do_not_create_extra_rows(terminator):
    source = f"a{terminator}1{terminator}2".encode() if terminator else b"a"
    result = CsvTextReader().read(source + terminator.encode())
    expected = ["a: 1", "a: 2"] if terminator else []
    assert texts(result) == expected


@pytest.mark.parametrize("delimiter", ["\t", ";", "|"])
def test_configured_delimiter(delimiter):
    source = f"a{delimiter}b\n1,5{delimiter}x\n".encode()
    result = CsvTextReader(delimiter=delimiter).read(source)
    assert texts(result) == ["a: 1,5\nb: x"]


def test_delimiter_is_not_guessed():
    result = CsvTextReader().read(b"a;b\n1;2\n")
    assert texts(result) == ["a;b: 1;2"]


@pytest.mark.parametrize(
    "source,message",
    [
        (b"", "Source contains no CSV rows."),
        (b"\n\n", "Source contains no CSV rows."),
        (b"a,b\n", "Source contains a header but no data rows."),
    ],
)
def test_empty_sources_succeed_with_an_explanation(source, message):
    result = CsvTextReader().read(source)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]
    assert result.findings[0].message == message
    assert result.findings[0].page_index == 0


def test_single_column_header_count_is_singular():
    result = CsvTextReader().read(b"a\n1,2\n")
    assert result.findings[0].message == (
        "Row starting on line 2 has 2 fields; the header has 1 field; "
        "it and later rows were not returned."
    )


def test_headerless_single_row_is_data():
    result = CsvTextReader(header=False).read(b"a,b\n")
    assert texts(result) == ["Column 1: a\nColumn 2: b"]


def test_malformed_row_keeps_earlier_rows_and_stops():
    source = b'a,b\n1,2\n3,"x"y\n5,6\n'
    result = CsvTextReader().read(source)
    assert result.outcome == "partial"
    assert texts(result) == ["a: 1\nb: 2"]
    assert codes(result) == ["parse_failed"]
    assert result.findings[0].message == (
        "Row starting on line 3 could not be parsed as CSV; "
        "it and later rows were not returned."
    )
    assert "x" not in result.findings[0].message


def test_unterminated_quote_reports_the_line_where_the_row_starts():
    source = b'a\n1\n\n"open\nmore\n'
    result = CsvTextReader().read(source)
    assert result.outcome == "partial" and texts(result) == ["a: 1"]
    assert "line 4" in result.findings[0].message


@pytest.mark.parametrize("source", [b'"a"b\n1\n', b'a\n"x"y\n', b'a\n\n"open'])
def test_malformed_without_completed_rows_fails(source):
    result = CsvTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["parse_failed"]


def test_stdlib_field_limit_is_reported_not_changed():
    limit = csv.field_size_limit()
    source = b"a\n1\n" + b"x" * (limit + 1) + b"\n2\n"
    result = CsvTextReader().read(source)
    assert result.outcome == "partial" and texts(result) == ["a: 1"]
    assert codes(result) == ["field_too_large"]
    assert "line 3" in result.findings[0].message
    assert csv.field_size_limit() == limit


def test_nul_characters_are_field_content():
    result = CsvTextReader().read(b"a\nx\x00y\n")
    assert texts(result) == ["a: x\x00y"]


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"],
)
def test_declared_encodings(encoding):
    name = "Cafe" if encoding == "ascii" else "Café"
    source = f"name,id\n{name},00123\n".encode(encoding)
    result = CsvTextReader(encoding=encoding).read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == [f"name: {name}\nid: 00123"]


def test_default_utf8_keeps_a_bom_in_the_first_label():
    result = CsvTextReader().read(b"\xef\xbb\xbfname\nAda\n")
    assert texts(result) == ["﻿name: Ada"]
    sig = CsvTextReader(encoding="utf-8-sig").read(b"\xef\xbb\xbfname\nAda\n")
    assert texts(sig) == ["name: Ada"]


def test_decode_failure_is_source_wide_and_private():
    result = CsvTextReader().read(b"name\nprivate\xff\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = CsvTextReader()
    result = reader.read(tmp_path / "private-name.csv")
    assert result.outcome == "failed"
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


def test_permission_errors_are_private(tmp_path, monkeypatch):
    path = tmp_path / "orders.csv"
    path.write_bytes(b"a\n1\n")

    def denied(*args, **kwargs):
        raise PermissionError("private error detail")

    monkeypatch.setattr(type(path), "open", denied)
    result = CsvTextReader().read(path)
    assert codes(result) == ["source_unreadable"]
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_layout_rejection_precedes_io(tmp_path, monkeypatch, layout, selection):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = CsvTextReader().read(
        tmp_path / "missing.csv", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert set(codes(result)) == {"selection_unsupported"}
    assert result.findings[0].message == (
        "CsvTextReader reads whole CSV sources, not layout regions."
    )


def test_layout_argument_validation(tmp_path, layout):
    reader = CsvTextReader()
    source = tmp_path / "does-not-exist"
    result = reader.read(source, layout=layout, region_ids=[])
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=layout, region_ids=["unknown"])
    with pytest.raises(ValueError, match="duplicates"):
        reader.read(source, layout=layout, region_ids=["first", "first"])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")
    with pytest.raises(TypeError, match="sequence"):
        reader.read(source, layout=layout, region_ids="first")
    with pytest.raises(TypeError, match="only strings"):
        reader.read(source, layout=layout, region_ids=[1])


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"encoding": None}, TypeError),
        ({"encoding": "unknown-codec"}, ValueError),
        ({"header": 1}, TypeError),
        ({"header": None}, TypeError),
        ({"header": "yes"}, TypeError),
        ({"delimiter": None}, TypeError),
        ({"delimiter": b","}, TypeError),
        ({"delimiter": ""}, ValueError),
        ({"delimiter": ",,"}, ValueError),
        ({"delimiter": '"'}, ValueError),
        ({"delimiter": "\n"}, ValueError),
        ({"delimiter": "\r"}, ValueError),
    ],
)
def test_configuration_errors(kwargs, error):
    with pytest.raises(error):
        CsvTextReader(**kwargs)


def test_configuration_is_keyword_only():
    with pytest.raises(TypeError):
        CsvTextReader(False)  # type: ignore[misc]


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        CsvTextReader().read(BytesIO(b"a\n1\n"))


def test_does_not_import_optional_tabular_backends(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"pandas", "openpyxl", "unstructured"}:
            raise AssertionError(f"unexpected import of {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert texts(CsvTextReader().read(b"a\n1\n")) == ["a: 1"]
