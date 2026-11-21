"""Four JSON grouping strategies share input handling, not block boundaries."""

import json
import shutil
from decimal import Decimal
from io import BytesIO
from pathlib import Path
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
from raggae.documents.text_reader import (
    JqJsonTextReader,
    RecordJsonTextReader,
    SubtreeJsonTextReader,
    TextReader,
    TextReadResult,
    WholeJsonTextReader,
)
from raggae.documents.text_reader.json.utils import (
    dump_json,
    loads_json,
    pointer_parts,
    resolve_pointer,
)

KINDS = ("whole", "record", "subtree", "jq")


def make_reader(kind, **options):
    if kind == "whole":
        return WholeJsonTextReader(**options)
    if kind == "record":
        return RecordJsonTextReader(**options)
    if kind == "subtree":
        return SubtreeJsonTextReader(pointers=("",), **options)
    return JqJsonTextReader(".", **options)


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
                id="first", surface_id="page-1", kind="text", bbox=(10, 10, 100, 40)
            ),
        ),
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_common_inputs_and_result_contract(
    tmp_path, fake_jq, kind, source_kind, keyword
):
    text = '[{"name":"Café İstanbul","nested":{"amount":1.2300},"empty":[]},{"name":"Lin"}]\r\n'
    data = text.encode()
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = make_reader(kind)
    result = reader.read(source=source) if keyword else reader.read(source)
    assert isinstance(reader, TextReader)
    assert (
        result.reader == reader.name
        and result.outcome == "success"
        and not result.findings
    )
    assert len(result.blocks) == (2 if kind == "record" else 1)
    values = [json.loads(b.text, parse_float=Decimal) for b in result.blocks]
    document = json.loads(text, parse_float=Decimal)
    assert values == (document if kind == "record" else [document])
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
    assert not hasattr(reader, "policy")


@pytest.mark.parametrize(
    "source",
    ['  {\r\n "n": 1.2300, "empty": []\r\n}\n', "{}", "[]", "null", '""', "false"],
)
def test_whole_document_preserves_validated_source_text_exactly(source):
    result = WholeJsonTextReader().read(source.encode())
    assert result.outcome == "success" and not result.findings
    assert len(result.blocks) == 1 and result.blocks[0].text == source


def test_record_reader_keeps_nested_objects_arrays_and_scalars():
    source = b'[{"name":"Ada","address":{"city":"London"},"tags":["gold"]},{},[],null,false,0,""]'
    result = RecordJsonTextReader().read(source)
    assert result.outcome == "success" and not result.findings
    assert [json.loads(b.text) for b in result.blocks] == json.loads(source)
    assert len(result.blocks) == 7


def test_record_reader_selects_nested_array_without_guessing_fields():
    source = b'{"ignored":"x","data":{"orders":[{"id":"00123","cost":1.2300},{"id":"00234"}]}}'
    result = RecordJsonTextReader(array_pointer="/data/orders").read(source)
    assert result.outcome == "success"
    assert [b.text for b in result.blocks] == [
        '{"id":"00123","cost":1.2300}',
        '{"id":"00234"}',
    ]
    assert (
        RecordJsonTextReader().read(source).findings[0].code == "structure_unsupported"
    )


@pytest.mark.parametrize("source", [b"[]", b'{"rows":[]}'])
def test_empty_record_array_is_successful_and_explicit(source):
    reader = RecordJsonTextReader(
        array_pointer="/rows" if source.startswith(b"{") else ""
    )
    result = reader.read(source)
    assert result.outcome == "success" and not result.blocks
    assert result.findings[0].code == "empty"


@pytest.mark.parametrize(
    "source,code",
    [
        (b'{"rows":null}', "structure_unsupported"),
        (b'{"rows":{}}', "structure_unsupported"),
        (b"{}", "pointer_unresolved"),
    ],
)
def test_record_selection_errors_do_not_return_other_content(source, code):
    result = RecordJsonTextReader(array_pointer="/rows").read(source)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == code


def test_subtree_reader_keeps_selected_values_in_request_order():
    source = b'{"customer":{"name":"Ada","address":{"city":"London"}},"orders":[{"id":1}],"ignored":"x"}'
    result = SubtreeJsonTextReader(pointers=("/orders/0", "/customer")).read(source)
    assert result.outcome == "success"
    assert [json.loads(b.text) for b in result.blocks] == [
        {"id": 1},
        {"name": "Ada", "address": {"city": "London"}},
    ]


def test_subtree_values_include_empty_containers_and_falsy_scalars():
    source = b'{"a":{},"b":[],"c":null,"d":false,"e":0,"f":""}'
    result = SubtreeJsonTextReader(pointers=("/a", "/b", "/c", "/d", "/e", "/f")).read(
        source
    )
    assert result.outcome == "success"
    assert [b.text for b in result.blocks] == ["{}", "[]", "null", "false", "0", '""']


def test_missing_subtree_preserves_independent_results_without_leaking_pointer():
    reader = SubtreeJsonTextReader(pointers=("/name", "/private-missing"))
    result = reader.read(b'{"name":"Ada"}')
    assert result.outcome == "partial" and [b.text for b in result.blocks] == ['"Ada"']
    assert result.findings[0].code == "pointer_unresolved"
    assert "index 1" in result.findings[0].message
    assert "private" not in result.model_dump_json()
    failed = SubtreeJsonTextReader(pointers=("/private-missing",)).read(b"{}")
    assert failed.outcome == "failed" and not failed.blocks


def test_empty_pointer_selection_does_not_open_source(tmp_path):
    result = SubtreeJsonTextReader(pointers=()).read(tmp_path / "missing.json")
    assert result.outcome == "success" and result.blocks == result.findings == ()


def test_mutating_callers_pointer_list_does_not_change_selection():
    pointers = ["/a"]
    reader = SubtreeJsonTextReader(pointers=pointers)
    pointers.append("/b")
    assert [b.text for b in reader.read(b'{"a":1,"b":2}').blocks] == ["1"]


@pytest.mark.parametrize(
    "pointers", [("/a", "/a"), ("/a", "/a/b"), ("/a/b", "/a"), ("", "/a")]
)
def test_overlapping_subtree_requests_are_caller_errors(pointers):
    with pytest.raises(ValueError, match="overlap"):
        SubtreeJsonTextReader(pointers=pointers)


@pytest.mark.parametrize("pointer", ["a", "#/a", "/a~", "/a~2"])
def test_bad_pointer_syntax_is_a_configuration_error(pointer):
    with pytest.raises(ValueError, match="pointer"):
        RecordJsonTextReader(array_pointer=pointer)
    with pytest.raises(ValueError, match="pointer"):
        SubtreeJsonTextReader(pointers=(pointer,))


@pytest.mark.parametrize("pointers", [None, 42, "/a", b"/a", [None]])
def test_subtree_pointer_types_are_checked(pointers):
    with pytest.raises(TypeError):
        SubtreeJsonTextReader(pointers=pointers)


@pytest.mark.parametrize(
    "pointer,value,expected",
    [
        ("", {"a": 1}, {"a": 1}),
        ("/a~1b/~0key/", {"a/b": {"~key": {"": 1}}}, 1),
        ("/a~01", {"a~1": 2}, 2),
        ("/01", {"01": 3}, 3),
        ("/0", ["first"], "first"),
        ("/a", {"a": None}, None),
    ],
)
def test_json_pointer_resolution(pointer, value, expected):
    assert resolve_pointer(value, pointer_parts(pointer)) == expected


@pytest.mark.parametrize("pointer", ["/01", "/-", "/-1", "/١", "/1", "/" + "9" * 5000])
def test_invalid_or_out_of_bounds_array_indexes_do_not_resolve(pointer):
    with pytest.raises(LookupError):
        resolve_pointer([1], pointer_parts(pointer))


@pytest.mark.parametrize(
    "source",
    [
        '{"n":1.2300,"array":[-0,{},[],null,true,"1.2300"]}',
        '["Café",{"a/b":[1E+02,1e10000]}]',
        '"quoted\\nline"',
        '"\\ud800"',
    ],
)
def test_subtree_encoding_preserves_values_and_exact_numeric_tokens(source):
    encoded = dump_json(loads_json(source))
    assert json.loads(encoded, parse_int=Decimal, parse_float=Decimal) == json.loads(
        source, parse_int=Decimal, parse_float=Decimal
    )
    if "1.2300" in source:
        assert '"n":1.2300' in encoded and '"1.2300"' in encoded
        assert "-0" in encoded


def test_encoder_does_not_add_a_recursive_walking_limit():
    source = "[" * 700 + "1.2300" + "]" * 700
    assert dump_json(loads_json(source)) == source


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "encoding,value",
    [
        ("utf-8", "Café"),
        ("utf-8-sig", "Café"),
        ("utf-16", "Café"),
        ("utf-32", "Café"),
        ("cp1252", "Café"),
        ("iso-8859-1", "Café"),
        ("ascii", "Plain"),
    ],
)
def test_all_workers_honor_source_encoding(fake_jq, kind, encoding, value):
    source = json.dumps([{"name": value}], ensure_ascii=False).encode(encoding)
    result = make_reader(kind, encoding=encoding).read(source)
    assert result.outcome == "success" and value in json.dumps(
        [json.loads(b.text) for b in result.blocks], ensure_ascii=False
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "source,code",
    [
        (b"", "parse_failed"),
        (b'{"x":}', "parse_failed"),
        (b"NaN", "parse_failed"),
        (b'{"private":1,"private":2}', "duplicate_key"),
        (b"\xff", "decode_failed"),
    ],
)
def test_all_workers_use_strict_document_decoding(fake_jq, kind, source, code):
    result = make_reader(kind).read(source)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == code
    assert "private" not in result.model_dump_json()
    assert fake_jq == []


@pytest.mark.parametrize("kind", KINDS)
def test_all_workers_report_unreadable_sources_without_path_leaks(
    tmp_path, fake_jq, kind
):
    result = make_reader(kind).read(tmp_path / "private-path.json")
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "source_unreadable"
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("kind", KINDS)
def test_layout_requests_reject_before_io(tmp_path, monkeypatch, fake_jq, layout, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported layout selection must not access source")

    monkeypatch.setattr(Path, "stat", forbidden)
    result = make_reader(kind).read(tmp_path / "missing.json", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].region_id == "first"
    assert result.findings[0].code == "selection_unsupported"
    assert fake_jq == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_layout_is_refused_even_when_nothing_is_selected(
    tmp_path, fake_jq, layout, kind
):
    result = make_reader(kind).read(
        tmp_path / "missing.json", layout=layout, region_ids=()
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]
    assert fake_jq == []


@pytest.mark.parametrize("kind", KINDS)
def test_common_configuration_errors_are_not_document_failures(fake_jq, layout, kind):
    with pytest.raises(TypeError):
        make_reader(kind).read(BytesIO(b"[]"))
    with pytest.raises(ValueError, match="require a layout"):
        make_reader(kind).read(b"[]", region_ids=())
    with pytest.raises(ValueError, match="belong"):
        make_reader(kind).read(b"[]", layout=layout, region_ids=("missing",))
    with pytest.raises(ValueError, match="encoding"):
        make_reader(kind, encoding="invalid")
    unusable = LayoutAnalysisResult(
        outcome="failed", findings=(LayoutFinding(code="failed", message="No map."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        make_reader(kind).read(b"[]", layout=unusable)


def test_jq_process_receives_stdin_and_separate_arguments_without_a_shell(fake_jq):
    source = b'{"name":"$(not-a-command)","empty":[]}'
    result = JqJsonTextReader(".", executable=Path("/custom/jq")).read(source)
    assert result.outcome == "success"
    command, options = fake_jq[0]
    assert command == [
        "/mock/jq",
        "--compact-output",
        "--monochrome-output",
        "--ascii-output",
        "--",
        ".",
    ]
    assert options["input"] == source.decode() and options["shell"] is False
    assert options["encoding"] == "utf-8" and options["errors"] == "strict"
    assert options["check"] is False


def test_missing_jq_has_an_install_instruction(monkeypatch):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(module.shutil, "which", lambda executable: None)
    with pytest.raises(FileNotFoundError, match="Install jq"):
        JqJsonTextReader(".").read(b"{}")


@pytest.mark.parametrize(
    "returncode,stdout,code",
    [
        (5, "1\n", "query_failed"),
        (0, "1\nnot-json\n", "query_failed"),
        (2, "", "query_failed"),
    ],
)
def test_jq_failure_does_not_return_partial_stdout_or_raw_stderr(
    monkeypatch, fake_jq, returncode, stdout, code
):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="private input or query diagnostics",
        ),
    )
    result = JqJsonTextReader(".").read(b"{}")
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == code
    assert "private" not in result.model_dump_json()


def test_jq_compilation_error_is_a_configuration_error(monkeypatch, fake_jq):
    import raggae.documents.text_reader.json.jq as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=3, stdout="", stderr="private expression"
        ),
    )
    with pytest.raises(ValueError, match="could not be compiled"):
        JqJsonTextReader("[").read(b"{}")


def test_jq_unicode_separator_inside_a_string_is_not_result_framing(
    monkeypatch, fake_jq
):
    import raggae.documents.text_reader.json.jq as module

    output = json.dumps("Ada\u2028Lin", ensure_ascii=False) + "\nnull\n"
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    result = JqJsonTextReader(".").read(b"{}")
    assert result.outcome == "success"
    assert [json.loads(b.text) for b in result.blocks] == ["Ada\u2028Lin", None]


@pytest.mark.parametrize(
    "expression,error",
    [(None, TypeError), (1, TypeError), ("", ValueError), (" \n", ValueError)],
)
def test_jq_expression_type_and_empty_configuration(expression, error):
    with pytest.raises(error):
        JqJsonTextReader(expression)


@pytest.mark.parametrize(
    "executable,error", [(123, TypeError), ("", ValueError), ("  ", ValueError)]
)
def test_jq_executable_configuration(executable, error):
    with pytest.raises(error):
        JqJsonTextReader(".", executable=executable)


@pytest.mark.parametrize(
    "expression,expected",
    [
        (
            ".items[]",
            [
                {"name": "Ada", "qty": 2, "price": 10},
                {"name": "Lin", "qty": 3, "price": 5},
            ],
        ),
        (
            ".items[] | {name, total: (.qty * .price)}",
            [{"name": "Ada", "total": 20}, {"name": "Lin", "total": 15}],
        ),
        (".items[].name", ["Ada", "Lin"]),
        ("empty", []),
        ("null, false, 0, {}, []", [None, False, 0, {}, []]),
    ],
)
def test_real_jq_queries(expression, expected):
    executable = shutil.which("jq")
    if executable is None:
        pytest.skip("optional local jq executable is not installed")
    source = b'{"items":[{"name":"Ada","qty":2,"price":10},{"name":"Lin","qty":3,"price":5}]}'
    result = JqJsonTextReader(expression, executable=executable).read(source)
    assert result.outcome == "success"
    assert [json.loads(b.text) for b in result.blocks] == expected
    assert bool(result.findings) == (not expected)


def test_real_jq_runtime_failure_is_atomic():
    executable = shutil.which("jq")
    if executable is None:
        pytest.skip("optional local jq executable is not installed")
    result = JqJsonTextReader('., error("private-source")', executable=executable).read(
        b'{"name":"Ada"}'
    )
    assert result.outcome == "failed" and not result.blocks
    assert "private" not in result.model_dump_json()
