"""CSV rows become XML <row> elements keyed by header names, with exact string values."""

import xml.etree.ElementTree as ET
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
    CsvXmlTextReader,
    TextContentFormat,
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


def rows(result):
    """Each block parsed as XML: [(tag, text), ...] per row."""
    parsed = []
    for block in result.blocks:
        root = ET.fromstring(block.text)
        assert root.tag == "row"
        parsed.append([(child.tag, child.text or "") for child in root])
    return parsed


def codes(result):
    return [finding.code for finding in result.findings]


def test_csv_family_exports_the_same_worker():
    from raggae.documents.text_reader import csv as csv_readers

    assert csv_readers.CsvXmlTextReader is CsvXmlTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = (
        "item,quantity,amount\r\nShipping labels,600,90\r\nCafé İstanbul,1,5\r\n"
    ).encode()
    path = tmp_path / "orders.csv"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path),
        "artifact_bytes": Artifact(data=data),
    }[source_kind]
    reader = CsvXmlTextReader()
    assert isinstance(reader, TextReader) and reader.name == "text_reader_csv_xml"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert rows(result) == [
        [("item", "Shipping labels"), ("quantity", "600"), ("amount", "90")],
        [("item", "Café İstanbul"), ("quantity", "1"), ("amount", "5")],
    ]
    assert all(
        b.content_format is TextContentFormat.XML
        and b.page_index == 0
        and b.region_id is None
        and b.method == "native"
        for b in result.blocks
    )
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_output_is_compact_and_keeps_column_order():
    result = CsvXmlTextReader().read(b"b,a\n1,2\n")
    assert [b.text for b in result.blocks] == ["<row><b>1</b><a>2</a></row>"]


@pytest.mark.parametrize("value", ["00123", "1e3", "-0", "true", "null", " 7 ", "1,5"])
def test_values_stay_exact_text(value):
    result = CsvXmlTextReader().read(f'id\n"{value}"\n'.encode())
    assert rows(result) == [[("id", value)]]


def test_markup_characters_are_escaped_and_round_trip():
    value = "a & b < c > d \"quoted\" 'single' &amp; <tag>"
    result = CsvXmlTextReader().read(
        f'id,note\n1,"{value.replace(chr(34), chr(34) * 2)}"\n'.encode()
    )
    assert result.outcome == "success"
    assert rows(result) == [[("id", "1"), ("note", value)]]
    assert "<tag>" not in result.blocks[0].text


def test_line_breaks_tabs_and_carriage_returns_round_trip():
    value = "one\ntwo\tthree\r\nfour"
    result = CsvXmlTextReader().read(f'note\n"{value}"\n'.encode())
    assert rows(result) == [[("note", value)]]
    assert "&#13;" in result.blocks[0].text  # A bare CR would be normalized away.


def test_empty_fields_are_empty_elements():
    result = CsvXmlTextReader().read(b"item,qty\nLabels,\n")
    assert [b.text for b in result.blocks] == ["<row><item>Labels</item><qty/></row>"]
    assert rows(result) == [[("item", "Labels"), ("qty", "")]]


def test_header_names_are_kept_verbatim_as_tags():
    result = CsvXmlTextReader().read(
        b"a,A,_x,item-1,item.2,\xc3\xa9t\xc3\xa9\n1,2,3,4,5,6\n"
    )
    assert [tag for tag, _ in rows(result)[0]] == [
        "a",
        "A",
        "_x",
        "item-1",
        "item.2",
        "été",
    ]


def test_duplicate_header_names_are_renamed_with_numbered_suffixes():
    result = CsvXmlTextReader().read(b"item,item,qty,item\nA,B,C,D\n")
    assert result.outcome == "success"
    assert rows(result) == [
        [("item", "A"), ("item_2", "B"), ("qty", "C"), ("item_3", "D")]
    ]
    assert codes(result) == ["header_renamed", "header_renamed"]
    assert [f.page_index for f in result.findings] == [0, 0]


def test_renaming_never_collides_with_a_real_column_name():
    result = CsvXmlTextReader().read(b"a,a,a_2\n1,2,3\n")
    assert [tag for tag, _ in rows(result)[0]] == ["a", "a_3", "a_2"]


@pytest.mark.parametrize("header", [b"name,,city", b"name,   ,city", b",name"])
def test_blank_header_name_fails_the_read(header):
    result = CsvXmlTextReader().read(header + b"\nA,B,C\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["header_blank"]


@pytest.mark.parametrize(
    "name", ["unit price", "2024", "a:b", "a&b", "-x", ".x", "x<y", "a/b", 'q"', "x\ty"]
)
def test_invalid_xml_names_fail_before_any_row(name):
    result = CsvXmlTextReader().read(f"id,{name}\n1,2\n".encode())
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["header_invalid"]
    assert "Header column 2" in result.findings[0].message
    assert name not in result.model_dump_json()


def test_an_invalid_name_fails_before_later_malformed_rows():
    result = CsvXmlTextReader().read(b'unit price\n"broken\n')
    assert codes(result) == ["header_invalid"]


def test_headerless_rows_use_value_elements_and_may_vary_in_length():
    result = CsvXmlTextReader(header=False).read(b"item,qty\nLabels\nTape,18,x\n")
    assert result.outcome == "success" and not result.findings
    assert [b.text for b in result.blocks] == [
        "<row><value>item</value><value>qty</value></row>",
        "<row><value>Labels</value></row>",
        "<row><value>Tape</value><value>18</value><value>x</value></row>",
    ]


def test_headerless_first_row_with_invalid_names_is_just_data():
    result = CsvXmlTextReader(header=False).read(b"unit price,2024,\n")
    assert rows(result) == [[("value", "unit price"), ("value", "2024"), ("value", "")]]


@pytest.mark.parametrize("bad_row,count", [(b"3", 1), (b"3,4,5", 3), (b'"3"', 1)])
def test_row_length_mismatch_keeps_earlier_rows_and_stops(bad_row, count):
    result = CsvXmlTextReader().read(b"a,b\n1,2\n" + bad_row + b"\n6,7\n")
    assert result.outcome == "partial"
    assert rows(result) == [[("a", "1"), ("b", "2")]]
    assert codes(result) == ["row_length_mismatch"]
    assert "line 3" in result.findings[0].message
    assert f"{count} field" in result.findings[0].message


def test_row_length_mismatch_without_completed_rows_fails():
    result = CsvXmlTextReader().read(b"a,b\n3\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["row_length_mismatch"]


def test_malformed_row_keeps_earlier_rows_and_stops():
    result = CsvXmlTextReader().read(b'a\n1\n"x"y\n3\n')
    assert result.outcome == "partial"
    assert rows(result) == [[("a", "1")]]
    assert codes(result) == ["parse_failed"]


@pytest.mark.parametrize("source", [b'"a"b\n1\n', b'a\n"x"y\n', b'a\n\n"open'])
def test_malformed_without_completed_rows_fails(source):
    result = CsvXmlTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["parse_failed"]


@pytest.mark.parametrize("char", ["\x00", "\x0b", "\x0c", "\x1f", "￾", "￿"])
def test_characters_xml_cannot_hold_stop_the_read(char):
    data = f"item,qty\nOK,1\nbad{char}value,2\nOK,3\n".encode()
    result = CsvXmlTextReader().read(data)
    assert result.outcome == "partial"
    assert rows(result) == [[("item", "OK"), ("qty", "1")]]
    assert codes(result) == ["invalid_character"]
    assert "line 3" in result.findings[0].message
    assert "bad" not in result.model_dump_json()


def test_an_illegal_character_in_the_first_data_row_fails():
    result = CsvXmlTextReader().read("item\nbad\x00\n".encode())
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["invalid_character"]


def test_allowed_whitespace_and_supplementary_characters_are_kept():
    value = "tab\tnl\nemoji \U0001f30d nbsp"
    result = CsvXmlTextReader().read(f'note\n"{value}"\n'.encode())
    assert result.outcome == "success"
    assert rows(result) == [[("note", value)]]


def test_rename_findings_are_kept_when_a_later_row_stops_the_read():
    result = CsvXmlTextReader().read(b"a,a\n1,2\n3\n")
    assert result.outcome == "partial"
    assert codes(result) == ["header_renamed", "row_length_mismatch"]


@pytest.mark.parametrize(
    "source,header,message",
    [
        (b"", True, "no CSV rows"),
        (b"\n\n", True, "no CSV rows"),
        (b"a,b\n", True, "header but no data rows"),
        (b"", False, "no CSV rows"),
    ],
)
def test_empty_sources_succeed_with_an_explanation(source, header, message):
    result = CsvXmlTextReader(header=header).read(source)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"] and message in result.findings[0].message


def test_blank_lines_are_skipped_including_before_the_header():
    result = CsvXmlTextReader().read(b"\n\na,b\n\n1,2\n\n3,4\n")
    assert rows(result) == [[("a", "1"), ("b", "2")], [("a", "3"), ("b", "4")]]


@pytest.mark.parametrize("delimiter", ["\t", ";", "|"])
def test_configured_delimiter(delimiter):
    data = f"a{delimiter}b\n1,5{delimiter}2\n".encode()
    result = CsvXmlTextReader(delimiter=delimiter).read(data)
    assert rows(result) == [[("a", "1,5"), ("b", "2")]]


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"],
)
def test_declared_encodings(encoding):
    name = "Cafe" if encoding == "ascii" else "Café"
    source = f"name,id\n{name},00123\n".encode(encoding)
    result = CsvXmlTextReader(encoding=encoding).read(source)
    assert result.outcome == "success" and not result.findings
    assert rows(result) == [[("name", name), ("id", "00123")]]


def test_decode_failure_is_source_wide_and_private():
    result = CsvXmlTextReader().read(b"name\nprivate\xff\n")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = CsvXmlTextReader()
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
    result = CsvXmlTextReader().read(
        tmp_path / "does-not-exist", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert set(codes(result)) == {"selection_unsupported"}
    assert result.findings[0].message == (
        "CsvXmlTextReader reads whole CSV sources, not layout regions."
    )


def test_a_layout_is_refused_even_when_nothing_is_selected(tmp_path, layout):
    result = CsvXmlTextReader().read(
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
        CsvXmlTextReader(**kwargs)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        CsvXmlTextReader().read(BytesIO(b"a\n1\n"))
