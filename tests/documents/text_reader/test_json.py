"""JSON scalar paths and values are content, not additional result fields."""

import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from stat import S_IFIFO, S_IFREG
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import JsonTextReader, TextReader, TextReadResult


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
                id="first", surface_id="page-1", kind="text", bbox=(10, 10, 100, 40)
            ),
            LayoutRegion(
                id="second", surface_id="page-1", kind="text", bbox=(10, 50, 100, 80)
            ),
        ),
    )


@pytest.mark.parametrize(
    "kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_common_shape_and_unchanged_file(tmp_path, kind, keyword):
    data = '{"customer":{"name":"Café İstanbul"},"orders":[{"id":"00123","amount":90}]}\r\n'.encode()
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[kind]
    reader = JsonTextReader()
    result = reader.read(source=source) if keyword else reader.read(source)
    assert isinstance(reader, TextReader)
    assert result.reader == reader.name == "text_reader_json"
    assert result.outcome == "success" and not result.findings
    assert [json.loads(b.text) for b in result.blocks] == [
        {"path": "/customer/name", "value": "Café İstanbul"},
        {"path": "/orders/0/id", "value": "00123"},
        {"path": "/orders/0/amount", "value": 90},
    ]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.content_format == "json" and block.method == "native"
        assert set(block.model_dump()) == {
            "text",
            "page_index",
            "region_id",
            "content_format",
            "method",
        }
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    assert "private-password" not in result.model_dump_json()
    if isinstance(source, Artifact):
        assert source.password == "private-password"


def test_paths_escape_object_keys_and_distinguish_array_indexes():
    data = b'{"a/b":{"~key":{"":true}},"0":"object-key","items":["first",{"name":"second"}]}'
    result = JsonTextReader().read(data)
    assert result.outcome == "success"
    assert [json.loads(b.text) for b in result.blocks] == [
        {"path": "/a~1b/~0key/", "value": True},
        {"path": "/0", "value": "object-key"},
        {"path": "/items/0", "value": "first"},
        {"path": "/items/1/name", "value": "second"},
    ]


@pytest.mark.parametrize(
    "source,value,kind",
    [
        (b'"Ada"', "Ada", str),
        (b'""', "", str),
        (b'"00123"', "00123", str),
        (b'"null"', "null", str),
        (b"null", None, type(None)),
        (b"false", False, bool),
        (b"true", True, bool),
        (b"0", 0, int),
        (b"1.5", 1.5, float),
    ],
)
def test_root_scalars_keep_types_and_use_empty_root_pointer(source, value, kind):
    result = JsonTextReader().read(source)
    assert result.outcome == "success" and not result.findings
    (block,) = result.blocks
    payload = json.loads(block.text)
    assert payload["path"] == "" and payload["value"] == value
    assert type(payload["value"]) is kind


def test_false_zero_null_and_empty_string_are_not_discarded_or_confused():
    result = JsonTextReader().read(b'[false,0,null,"",true,"false"]')
    payloads = [json.loads(b.text) for b in result.blocks]
    assert [p["path"] for p in payloads] == ["/0", "/1", "/2", "/3", "/4", "/5"]
    assert [type(p["value"]) for p in payloads] == [
        bool,
        int,
        type(None),
        str,
        bool,
        str,
    ]
    assert [p["value"] for p in payloads] == [False, 0, None, "", True, "false"]


@pytest.mark.parametrize(
    "token",
    [
        "1.2300",
        "-0",
        "0.00000000000000000001",
        "1E+02",
        "1e10000",
        "90071992547409931234567890",
        "9" * 5000,
    ],
)
def test_numeric_tokens_are_not_rounded_reformatted_or_stringified(token):
    result = JsonTextReader().read(('{"n":' + token + "}").encode())
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == '{"path": "/n", "value": ' + token + "}"
    payload = json.loads(result.blocks[0].text, parse_int=Decimal, parse_float=Decimal)
    assert payload["value"] == Decimal(token)


def test_source_strings_that_look_like_numbers_stay_quoted_strings():
    result = JsonTextReader().read(b'{"number":1.2300,"string":"1.2300"}')
    assert result.blocks[0].text == '{"path": "/number", "value": 1.2300}'
    assert result.blocks[1].text == '{"path": "/string", "value": "1.2300"}'


@pytest.mark.parametrize("source", [b"{}", b"[]", b'{"a":[],"b":{}}', b"[{},[]]"])
def test_valid_containers_without_scalars_are_successful_and_explicit(source):
    result = JsonTextReader().read(source)
    assert result.outcome == "success" and result.blocks == ()
    (finding,) = result.findings
    assert finding.code == "empty" and finding.page_index == 0


def test_empty_containers_do_not_create_invented_scalar_values():
    result = JsonTextReader().read(b'{"empty":{},"name":"Ada","items":[],"n":0}')
    assert result.outcome == "success" and not result.findings
    assert [json.loads(b.text) for b in result.blocks] == [
        {"path": "/name", "value": "Ada"},
        {"path": "/n", "value": 0},
    ]


@pytest.mark.parametrize(
    "source",
    [
        b'{"private-key":1,"private-key":2}',
        b'{"valid":"private-value","nested":{"a":1,"a":2}}',
        rb'{"a":1,"\u0061":2}',
    ],
)
def test_duplicate_keys_fail_the_whole_document_without_leaking_values(source):
    result = JsonTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "duplicate_key"
    assert result.findings[0].page_index == 0
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b" \r\n\t",
        b'{"private-key":}',
        b'{"a":1} trailing',
        b'{"a":1,}',
        b"[1,]",
        b"01",
        b"+1",
        b"1.",
        b".5",
        b"1e",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"[1,NaN]",
        b'{"x":"bad\x00string"}',
        b'{"valid":"private-value"}\n{"extra":1}',
    ],
)
def test_invalid_json_fails_without_partial_values_or_raw_diagnostics(source):
    result = JsonTextReader().read(source)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "parse_failed"
    assert result.findings[0].page_index == 0
    assert "private" not in result.model_dump_json()


def test_json_string_escaping_and_unicode_keep_serializable_content():
    original = {
        'a"/\n~': 'Café 🌍\n"quoted"\\path',
        "script": "<script>not executed</script>",
    }
    result = JsonTextReader().read(json.dumps(original, ensure_ascii=False).encode())
    assert result.outcome == "success"
    assert [json.loads(b.text) for b in result.blocks] == [
        {"path": '/a"~1\n~0', "value": original['a"/\n~']},
        {"path": "/script", "value": original["script"]},
    ]
    escaped = JsonTextReader().read(rb'{"\ud800":"\udfff"}')
    assert escaped.outcome == "success"
    assert TextReadResult.model_validate_json(escaped.model_dump_json()) == escaped
    assert json.loads(escaped.blocks[0].text) == {"path": "/\ud800", "value": "\udfff"}


@pytest.mark.parametrize(
    "encoding,value",
    [
        ("utf-8", "Café 🌍"),
        ("utf-8-sig", "Café 🌍"),
        ("utf-16", "Café 🌍"),
        ("utf-32", "Café 🌍"),
        ("cp1252", "Café €"),
        ("iso-8859-1", "Café"),
        ("ascii", "Plain"),
    ],
)
def test_explicit_source_encoding(encoding, value):
    source = json.dumps({"name": value}, ensure_ascii=False).encode(encoding)
    result = JsonTextReader(encoding=encoding).read(source)
    assert result.outcome == "success"
    assert json.loads(result.blocks[0].text) == {"path": "/name", "value": value}


def test_utf8_bom_requires_the_explicit_sig_encoding():
    source = '{"name":"Ada"}'.encode("utf-8-sig")
    assert JsonTextReader().read(source).findings[0].code == "parse_failed"
    assert JsonTextReader(encoding="utf-8-sig").read(source).outcome == "success"


@pytest.mark.parametrize("data", [b'"Caf\xe9"', b"\xff", b'{"x":"valid"}\xff'])
def test_invalid_encoding_fails_without_guessing(data):
    result = JsonTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "decode_failed"
    assert result.findings[0].page_index is None


@pytest.mark.parametrize(
    "encoding,error",
    [
        ("unknown", ValueError),
        ("base64_codec", ValueError),
        ("", ValueError),
        ("UTF-8", ValueError),
        (None, TypeError),
        (12, TypeError),
        (b"utf-8", TypeError),
    ],
)
def test_invalid_encoding_is_a_configuration_error(encoding, error):
    with pytest.raises(error, match="encoding must be"):
        JsonTextReader(encoding=encoding)


@pytest.mark.parametrize("file_backed", [False, True])
def test_complete_json_beyond_former_reader_limits(tmp_path, file_backed):
    value = "x" * (10 * 1024 * 1024) + " 🌍"
    source = json.dumps({"value": value}, ensure_ascii=False).encode()
    if file_backed:
        path = tmp_path / "large.json"
        path.write_bytes(source)
        source = path
    result = JsonTextReader().read(source)
    assert result.outcome == "success" and not result.findings
    assert json.loads(result.blocks[0].text) == {"path": "/value", "value": value}
    assert not hasattr(JsonTextReader(), "policy")


def test_file_read_is_complete_and_handle_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "source.json"
    path.write_bytes(b"x")
    stream = BytesIO(b'{"name":"Ada"}')
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=S_IFREG, st_size=60 * 1024 * 1024
        ),
    )
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    assert JsonTextReader().read(path).outcome == "success"
    assert stream.closed


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable", "pipe"])
def test_unreadable_source_is_a_safe_failure(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-source.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_bytes(b"{}")

        def denied(*args, **kwargs):
            raise PermissionError("private error")

        monkeypatch.setattr(Path, "open", denied)
    elif kind == "pipe":
        monkeypatch.setattr(
            Path, "stat", lambda *args, **kwargs: SimpleNamespace(st_mode=S_IFIFO)
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("Pipe must not be opened")

        monkeypatch.setattr(Path, "open", forbidden)
    result = JsonTextReader().read(Artifact(path=path, password="private-password"))
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "source_unreadable"
    assert result.findings[0].page_index is None
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_region_requests_reject_without_io(tmp_path, monkeypatch, layout, selection):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported selection must not read source")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = JsonTextReader().read(
        tmp_path / "missing.json", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(f.region_id for f in result.findings) == expected
    assert all(f.code == "selection_unsupported" for f in result.findings)


@pytest.mark.parametrize("selection", [(), []])
def test_a_layout_is_refused_even_when_nothing_is_selected(tmp_path, layout, selection):
    result = JsonTextReader().read(
        tmp_path / "missing.json", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


def test_a_map_without_regions_is_refused_too(tmp_path, layout):
    empty = LayoutAnalysisResult(outcome="success", surfaces=layout.surfaces)
    result = JsonTextReader().read(tmp_path / "missing.json", layout=empty)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize(
    "ids,error",
    [
        ("first", TypeError),
        ([None], TypeError),
        (42, TypeError),
        (("first", "first"), ValueError),
        (("missing",), ValueError),
    ],
)
def test_invalid_selection_is_a_caller_error(layout, ids, error):
    with pytest.raises(error):
        JsonTextReader().read(b"{}", layout=layout, region_ids=ids)


def test_ids_require_a_usable_layout():
    with pytest.raises(ValueError, match="require a layout"):
        JsonTextReader().read(b"{}", region_ids=())
    with pytest.raises(TypeError, match="layout must be"):
        JsonTextReader().read(b"{}", layout={})
    unusable = LayoutAnalysisResult(
        outcome="failed", findings=(LayoutFinding(code="failed", message="No map."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        JsonTextReader().read(b"{}", layout=unusable)


@pytest.mark.parametrize(
    "source", [None, 12, bytearray(b"{}"), BytesIO(b"{}"), {"name": "Ada"}]
)
def test_invalid_source_types_raise(source):
    with pytest.raises(TypeError, match="source must be"):
        JsonTextReader().read(source)


def test_decoder_recursion_error_is_a_safe_failure(monkeypatch):
    import raggae.documents.text_reader.json.scalars as worker

    def broken(*args, **kwargs):
        raise RecursionError("private details")

    monkeypatch.setattr(worker.json, "loads", broken)
    result = JsonTextReader().read(b'{"name":"Ada"}')
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "parse_failed"
    assert "private" not in result.model_dump_json()
