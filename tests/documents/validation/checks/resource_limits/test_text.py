import errno
import json
from pathlib import Path

import pytest

from raggae.documents.validation import TextLimits, TextResourceProbe
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code
from raggae.documents.validation.shared import TEXT_FORMATS


@pytest.mark.parametrize("format_name", ["json", "jsonl", " .JSON ", " .JSONL "])
@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize("depth", [2, 3, 4])
def test_depth_boundary_and_exact_facts(file_factory, format_name, as_string, depth):
    path = file_factory("input.json", b"[" * depth + b"0" + b"]" * depth)
    before = path.read_bytes()
    outcome = TextResourceProbe(TextLimits(max_nesting_depth=3)).check(str(path) if as_string else path, format_name)
    assert outcome.codes == ((Code.NESTING_TOO_DEEP,) if depth > 3 else ())
    assert dict(outcome.facts) == {"nesting_depth": depth}
    if depth > 3:
        assert dict(outcome.findings[0].detail) == {"limit": 3, "observed": 4}
    assert path.read_bytes() == before


@pytest.mark.parametrize("content,depth", [
    ("", 0), ("null", 0), ("true", 0), ("42", 0), ('"text [ {} ]"', 0),
    ("[]", 1), ("{}", 1), ("[{}]", 2), ('{"a":[{}]}', 3),
    ('{"brackets":"{{[[]]}}"}', 1),
    (json.dumps({"quote": 'escaped"[[[', "backslash": r"\\[[["}), 1),
    (r'["escaped\" [[[", {"x": []}]', 3),
    (r'["escaped\\", [[]]]', 3),
    ('[{}]\n{"x":[{}]}\n[]', 3),
    ('[]\n[[[0]]]', 3),
    ('}]]{{', 2), ('[[[', 3), ('"unfinished [[[[', 0),
])
@pytest.mark.parametrize("format_name", ["json", "jsonl"])
def test_strings_escape_state_records_and_malformed_syntax_are_measured(file_factory, content, depth, format_name):
    path = file_factory(content=content.encode("utf-8"))
    outcome = TextResourceProbe(TextLimits(max_nesting_depth=3)).check(path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"nesting_depth": depth}


@pytest.mark.parametrize("format_name", [None, "", *sorted(TEXT_FORMATS - {"json", "jsonl"}), " .MD "])
def test_non_json_text_and_none_do_not_read_or_infer_from_filename(monkeypatch, missing_path, forbidden_operation, format_name):
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    # A JSON filename alone is intentionally insufficient to select JSON scanning.
    outcome = TextResourceProbe().check(missing_path.with_suffix(".json"), format_name)
    if format_name == "":
        assert outcome.codes == (Code.NOT_APPLICABLE,)
    else:
        assert outcome.codes == ()
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("format_name,normalized", [("pdf", "pdf"), ("jpg", "jpeg"), (" .DOCX ", "docx")])
def test_mismatched_format_never_reads(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    outcome = TextResourceProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}


@pytest.mark.parametrize("bad_path", [None, 1, b"input.json", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        TextResourceProbe().check(bad_path, "json")


@pytest.mark.parametrize("format_name", ["json", "jsonl"])
def test_missing_json_input_is_unreadable(missing_path, format_name):
    outcome = TextResourceProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}


@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, IsADirectoryError])
def test_os_read_failure_returns_unreadable(monkeypatch, missing_path, error_type):
    def fail(*args, **kwargs):
        raise error_type(errno.EIO, "cannot read text")

    monkeypatch.setattr(Path, "read_text", fail)
    outcome = TextResourceProbe().check(missing_path, "json")
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}


@pytest.mark.parametrize("content,depth", [(b"[[\xff]]", 2), (b'{"a":"\xff [[["}', 1)])
def test_invalid_utf8_is_replaced_without_inventing_a_syntax_check(file_factory, monkeypatch, content, depth):
    path = file_factory(content=content)
    original_read = Path.read_text
    calls = []

    def read_text(target, *args, **kwargs):
        calls.append((target, args, kwargs))
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    outcome = TextResourceProbe().check(path, "json")
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"nesting_depth": depth}
    assert calls == [(path, (), {"encoding": "utf-8", "errors": "replace"})]


@pytest.mark.parametrize("format_name", ["json", "jsonl"])
def test_deep_input_is_scanned_without_parsing_or_recursion(file_factory, monkeypatch, forbidden_operation, format_name):
    path = file_factory(content=b"[" * 10_000 + b"0" + b"]" * 10_000)
    monkeypatch.setattr(json, "loads", forbidden_operation)
    monkeypatch.setattr(json, "load", forbidden_operation)
    outcome = TextResourceProbe().check(path, format_name)
    assert outcome.codes == (Code.NESTING_TOO_DEEP,)
    assert dict(outcome.facts) == {"nesting_depth": 10_000}
    assert dict(outcome.findings[0].detail) == {"limit": 100, "observed": 10_000}


@pytest.mark.parametrize("password", [None, "", "irrelevant"])
def test_password_is_irrelevant(file_factory, password):
    path = file_factory(content=b"[{}]")
    assert TextResourceProbe().check(path, "json", password) == TextResourceProbe().check(path, "json")


def test_repeated_calls_and_policy_are_instance_local(file_factory):
    policy = TextLimits(max_nesting_depth=1)
    probe = TextResourceProbe(policy)
    large = file_factory("large.json", b"[{}]")
    small = file_factory("small.json", b"[]")
    assert probe.check(large, "json").codes == (Code.NESTING_TOO_DEEP,)
    assert probe.check(small, "json").codes == ()
    assert probe.check(large, "json").facts["nesting_depth"] == 2
    assert TextResourceProbe().check(large, "json").codes == ()
    assert probe.policy is policy
    assert probe.name == "validation_resource_limits_text"
    assert probe.handles == frozenset(TEXT_FORMATS)
