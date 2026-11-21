"""Independent JSON Lines records keep their original line numbers."""

import json
import shutil
from decimal import Decimal
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
    JqJsonLinesTextReader,
    JsonLinesTextReader,
    TextReader,
    TextReadResult,
)


def make_reader(kind, **kwargs):
    if kind == "native":
        return JsonLinesTextReader(**kwargs)
    return JqJsonLinesTextReader(".", **kwargs)


@pytest.fixture
def fake_jq(monkeypatch):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(module.shutil, "which", lambda executable: "/mock/jq")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=kwargs["input"] + "\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


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
        ),
    )


def values(result):
    return [json.loads(block.text) for block in result.blocks]


def test_json_family_exports_the_same_json_lines_workers():
    from raggae.documents.text_reader import json as json_readers
    from raggae.documents.text_reader.json import jsonl

    assert (
        json_readers.JsonLinesTextReader
        is jsonl.JsonLinesTextReader
        is JsonLinesTextReader
    )
    assert (
        json_readers.JqJsonLinesTextReader
        is jsonl.JqJsonLinesTextReader
        is JqJsonLinesTextReader
    )
    assert (
        JsonLinesTextReader.__module__
        == "raggae.documents.text_reader.json.jsonl.lines"
    )
    assert (
        JqJsonLinesTextReader.__module__ == "raggae.documents.text_reader.json.jsonl.jq"
    )


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, fake_jq, kind, source_kind, keyword
):
    data = (
        '{"id":"00123","nested":{"name":"Café İstanbul"}}\r\n{"items":[]}\r\n'.encode()
    )
    path = tmp_path / "records.jsonl"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = make_reader(kind)
    assert isinstance(reader, TextReader)
    assert reader.name == (
        "text_reader_jsonl" if kind == "native" else "text_reader_jq_jsonl"
    )
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert values(result) == [
        {"line": 1, "value": {"id": "00123", "nested": {"name": "Café İstanbul"}}},
        {"line": 2, "value": {"items": []}},
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


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize(
    "value",
    [{}, [], None, False, True, 0, "", [1, {"a": 2}], "x\ny", "x\u2028y\u2029z"],
)
def test_any_json_value_is_a_record(fake_jq, kind, value):
    # Literal Unicode separators are string data, not JSON Lines delimiters.
    source = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    result = make_reader(kind).read(source)
    assert result.outcome == "success" and not result.findings
    assert values(result) == [{"line": 1, "value": value}]


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize("terminator", ["", "\n", "\r\n"])
def test_final_terminator_does_not_create_an_extra_record(fake_jq, kind, terminator):
    result = make_reader(kind).read(("{}\n[]" + terminator).encode())
    assert result.outcome == "success" and not result.findings
    assert values(result) == [{"line": 1, "value": {}}, {"line": 2, "value": []}]


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-json",
        '{"secret":',
        '{"a":1,"a":2}',
        "NaN",
        "Infinity",
        "{} {}",
        "{\r}",
    ],
)
def test_bad_records_do_not_erase_other_records(fake_jq, kind, bad):
    # {\r} is valid JSON whitespace, unlike a CR separating two JSON values.
    if bad == "{\r}":
        bad = "{}\r{}"
    result = make_reader(kind).read(("{}\n" + bad + '\n{"ok":true}\n').encode())
    assert result.outcome == "partial"
    assert values(result) == [
        {"line": 1, "value": {}},
        {"line": 3, "value": {"ok": True}},
    ]
    assert len(result.findings) == 1 and "Line 2" in result.findings[0].message
    suffix = "duplicate_key" if bad == '{"a":1,"a":2}' else "parse_failed"
    assert result.findings[0].code == suffix
    assert result.findings[0].page_index == 0
    assert "secret" not in result.model_dump_json()
    if kind == "jq":
        assert len(fake_jq) == 2  # Invalid records never reach jq.


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_all_invalid_records_fail_without_values(fake_jq, kind):
    result = make_reader(kind).read(b'broken\n{"a":1,"a":2}\n\n')
    assert result.outcome == "failed" and result.blocks == ()
    assert len(result.findings) == 3
    assert [f.message.split()[1] for f in result.findings] == ["1", "2", "3"]
    assert fake_jq == []


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_empty_source_succeeds_without_executing_jq(fake_jq, kind):
    result = make_reader(kind).read(b"")
    assert result.outcome == "success" and result.blocks == ()
    assert len(result.findings) == 1 and result.findings[0].code == "empty"
    assert fake_jq == []


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-32", "cp1252", "iso-8859-1", "ascii"],
)
def test_declared_encodings(fake_jq, kind, encoding):
    name = "Cafe" if encoding == "ascii" else "Café"
    source = ('{"name":"' + name + '"}\n{"id":2}\n').encode(encoding)
    result = make_reader(kind, encoding=encoding).read(source)
    assert result.outcome == "success" and not result.findings
    assert values(result) == [
        {"line": 1, "value": {"name": name}},
        {"line": 2, "value": {"id": 2}},
    ]


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_default_utf8_bom_fails_first_record_only(fake_jq, kind):
    result = make_reader(kind).read(b'\xef\xbb\xbf{}\n{"ok":true}\n')
    assert result.outcome == "partial"
    assert values(result) == [{"line": 2, "value": {"ok": True}}]
    assert result.findings[0].code == "parse_failed"


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_decode_failure_is_source_wide_and_private(fake_jq, kind):
    result = make_reader(kind).read(b'{}\n{"secret":"\xff"}\n')
    assert result.outcome == "failed" and result.blocks == ()
    assert result.findings[0].code == "decode_failed"
    assert result.findings[0].page_index is None
    assert "secret" not in result.model_dump_json() and fake_jq == []


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_missing_and_nonregular_files_are_not_opened(
    tmp_path, fake_jq, monkeypatch, kind
):
    reader = make_reader(kind)
    result = reader.read(tmp_path / "private-name.jsonl")
    assert result.outcome == "failed" and result.findings[0].code == "source_unreadable"
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"
    assert fake_jq == []


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_layout_refusal_including_an_empty_selection_precedes_io(
    tmp_path, fake_jq, kind, layout
):
    reader = make_reader(kind)
    source = tmp_path / "does-not-exist"
    result = reader.read(source, layout=layout)
    assert result.outcome == "failed" and result.blocks == ()
    assert result.findings[0].region_id == "first"
    assert result.findings[0].code == "selection_unsupported"
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
    assert fake_jq == []


@pytest.mark.parametrize("kind", ["native", "jq"])
@pytest.mark.parametrize(
    "encoding,error",
    [(None, TypeError), (123, TypeError), ("unknown-codec", ValueError)],
)
def test_encoding_config_errors(kind, encoding, error):
    with pytest.raises(error):
        make_reader(kind, encoding=encoding)


@pytest.mark.parametrize("kind", ["native", "jq"])
def test_streams_are_not_silently_added_to_source_contract(kind):
    with pytest.raises(TypeError, match="source must"):
        make_reader(kind).read(BytesIO(b"{}\n"))


def test_native_preserves_exact_numbers_nested_content_and_empty_containers():
    source = b'{"n":1.2300,"large":123456789012345678901234567890,"negative":-0,"e":1e+1000,"empty":[]}\n{}\n'
    result = JsonLinesTextReader().read(source)
    assert result.outcome == "success" and len(result.blocks) == 2
    assert (
        result.blocks[0].text
        == '{"line":1,"value":' + source.decode().split("\n")[0] + "}"
    )
    value = json.loads(result.blocks[0].text, parse_float=Decimal)["value"]
    assert value["n"] == Decimal("1.2300") and value["e"] == Decimal("1e+1000")
    assert value["large"] == 123456789012345678901234567890
    assert values(result)[1] == {"line": 2, "value": {}}


def test_jq_invokes_each_valid_record_independently_without_a_shell(fake_jq):
    result = JqJsonLinesTextReader(".items[]", executable="/custom/jq").read(
        b"{}\n[]\n"
    )
    assert result.outcome == "success" and len(fake_jq) == 2
    assert [kwargs["input"] for _, kwargs in fake_jq] == ["{}", "[]"]
    for command, kwargs in fake_jq:
        assert command == [
            "/mock/jq",
            "--compact-output",
            "--monochrome-output",
            "--ascii-output",
            "--",
            ".items[]",
        ]
        assert kwargs["shell"] is False and kwargs["check"] is False
        assert kwargs["encoding"] == "utf-8" and "timeout" not in kwargs


def test_jq_multiple_results_keep_original_line_and_output_order(monkeypatch, fake_jq):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout='"first"\nnull\nfalse\n0\n{}\n[]\n', stderr=""
        ),
    )
    result = JqJsonLinesTextReader(".").read(b"{}\ninvalid\n{}\n")
    assert result.outcome == "partial"
    assert values(result) == [
        {"line": line, "value": value}
        for line in (1, 3)
        for value in ("first", None, False, 0, {}, [])
    ]


@pytest.mark.parametrize("bad", ["runtime", "bad-output", "process"])
def test_jq_failed_record_is_atomic_and_later_records_continue(
    monkeypatch, fake_jq, bad
):
    import raggae.documents.text_reader.json.jq as module

    def run(command, **kwargs):
        if kwargs["input"] == "2":
            if bad == "process":
                raise OSError("private error")
            if bad == "bad-output":
                return SimpleNamespace(
                    returncode=0, stdout="1\nsecret-not-json\n", stderr="private error"
                )
            return SimpleNamespace(
                returncode=5, stdout='"discard-me"\n', stderr="private error"
            )
        return SimpleNamespace(returncode=0, stdout=kwargs["input"] + "\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = JqJsonLinesTextReader(".").read(b"1\n2\n3\n")
    assert result.outcome == "partial"
    assert values(result) == [{"line": 1, "value": 1}, {"line": 3, "value": 3}]
    assert len(result.findings) == 1 and "Line 2:" in result.findings[0].message
    assert result.findings[0].code == "query_failed"
    assert all(
        word not in result.model_dump_json()
        for word in ("private error", "secret-not-json", "discard-me")
    )


@pytest.mark.parametrize("invalid_line", [False, True])
def test_jq_empty_query_results_complete_records_even_without_blocks(
    monkeypatch, fake_jq, invalid_line
):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    result = JqJsonLinesTextReader("empty").read(
        b"{}\ninvalid\n" if invalid_line else b"{}\n"
    )
    assert result.outcome == ("partial" if invalid_line else "success")
    assert result.blocks == ()
    assert result.findings[0].code == "empty"
    assert "Line 1:" in result.findings[0].message


def test_jq_all_query_failures_return_failed(monkeypatch, fake_jq):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=5, stdout="", stderr=""),
    )
    result = JqJsonLinesTextReader('error("failed")').read(b"{}\n[]\n")
    assert result.outcome == "failed" and not result.blocks
    assert len(result.findings) == 2


def test_jq_missing_executable_and_compile_errors_are_config_errors(monkeypatch):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(module.shutil, "which", lambda executable: None)
    with pytest.raises(FileNotFoundError, match="local jq"):
        JqJsonLinesTextReader(".").read(b"{}\n")
    monkeypatch.setattr(module.shutil, "which", lambda executable: "/mock/jq")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=3, stdout="", stderr="source-secret"
        ),
    )
    with pytest.raises(ValueError, match="compiled") as exc:
        JqJsonLinesTextReader("invalid filter").read(b"{}\n")
    assert "source-secret" not in str(exc.value)


@pytest.mark.parametrize(
    "expression,error",
    [(None, TypeError), (3, TypeError), ("", ValueError), ("  ", ValueError)],
)
def test_jq_filter_config_is_validated_at_construction(expression, error):
    with pytest.raises(error):
        JqJsonLinesTextReader(expression)


@pytest.mark.parametrize(
    "executable,error", [(3, TypeError), ("", ValueError), ("  ", ValueError)]
)
def test_jq_executable_config_is_validated_at_construction(executable, error):
    with pytest.raises(error):
        JqJsonLinesTextReader(".", executable=executable)


@pytest.mark.parametrize(
    "expression,source,expected,outcome",
    [
        (
            ".items[]",
            b'{"items":[1,2]}\n{"items":[3]}\n',
            [{"line": 1, "value": 1}, {"line": 1, "value": 2}, {"line": 2, "value": 3}],
            "success",
        ),
        (
            "{item, total: (.qty * .price)}",
            b'{"item":"Book","qty":2,"price":10}\nbroken\n{"item":"Pen","qty":3,"price":5}\n',
            [
                {"line": 1, "value": {"item": "Book", "total": 20}},
                {"line": 3, "value": {"item": "Pen", "total": 15}},
            ],
            "partial",
        ),
        (
            ".items[]",
            b'{"items":[1]}\n{"items":null}\n{"items":[3]}\n',
            [{"line": 1, "value": 1}, {"line": 3, "value": 3}],
            "partial",
        ),
        ("empty", b"{}\n[]\n", [], "success"),
        (
            "null,false,0,{},[]",
            b"{}\n",
            [{"line": 1, "value": v} for v in (None, False, 0, {}, [])],
            "success",
        ),
        (
            'if .bad then (1, error("secret")) else .value end',
            b'{"value":1}\n{"bad":true}\n{"value":3}\n',
            [{"line": 1, "value": 1}, {"line": 3, "value": 3}],
            "partial",
        ),
    ],
)
def test_real_local_jq(expression, source, expected, outcome):
    if shutil.which("jq") is None:
        pytest.skip("optional local jq executable is not installed")
    result = JqJsonLinesTextReader(expression).read(source)
    assert result.outcome == outcome
    assert values(result) == expected
