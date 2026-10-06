import errno
from pathlib import Path
import urllib.request
import webbrowser

import pytest

from raggae.documents.validation import ActiveContentPolicy, CsvActiveContentProbe, Severity
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("content", [b"", b",", b"\n\r\n", b"name,age\nAda,42\n", b"hello", b"plain,words\r\n"])
@pytest.mark.parametrize("format_name", [None, "csv", " .CSV "])
@pytest.mark.parametrize("as_string", [False, True])
def test_clean_csv_formats_and_source_preservation(file_factory, content, format_name, as_string):
    path = file_factory("clean.csv", content)
    result = CsvActiveContentProbe().check(str(path) if as_string else path, format_name)
    assert result.codes == ()
    assert dict(result.facts) == {}
    assert path.read_bytes() == content


@pytest.mark.parametrize("value", ["-1", "+1", "-1.25", "+0.5", "-.5", "+.5", "-1e3", "+2E-4",
                                    "-0", "+0", "-inf", "+Infinity", "-NaN", "+nan",
                                    "'=1+1", "'+SUM(A1:A2)", "'-SUM(A1:A2)", "'@SUM(A1:A2)"])
def test_numeric_and_apostrophe_prefixed_values_are_not_formulas(file_factory, value):
    # Inf/NaN explicitly characterize the existing float-based numeric exemption.
    path = file_factory("numbers.csv", f"value\n{value}\n".encode("utf-8"))
    assert CsvActiveContentProbe().check(path, "csv").codes == ()


@pytest.mark.parametrize("formula", ["=1+1", "+SUM(A1:A2)", "-SUM(A1:A2)", "@SUM(A1:A2)"])
@pytest.mark.parametrize("padding", ["", " ", "\t", "  \t"])
@pytest.mark.parametrize("quote", ['', '"'])
def test_formula_prefixes_whitespace_and_simple_quotes(file_factory, formula, padding, quote):
    cell = f"{quote}{padding}{formula} {quote}"
    path = file_factory("formula.csv", f"name,value\nAda,{cell}\n".encode("utf-8"))
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 1, "sample": formula}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("value", ["+", "-", "@", "=", "-1.2.3", "+1e", "-SUM(A1:A2)"])
def test_lone_prefixes_and_malformed_numbers_are_reported(file_factory, value):
    path = file_factory("not-number.csv", value.encode("utf-8"))
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 1, "sample": value}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_occurrences_count_duplicates_and_keep_file_order(file_factory, newline):
    content = newline.join(["=1+1,plain,+SUM(A1:A2)", "=1+1,@SUM(A1:A2),-3"])
    path = file_factory("many.csv", content.encode("utf-8"))
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {
        "count": 4, "sample": "=1+1, +SUM(A1:A2), =1+1, @SUM(A1:A2)"}


@pytest.mark.parametrize("sample_size", [None, 0, 1, 5, 10])
def test_sample_limit_does_not_change_complete_count(file_factory, sample_size):
    formulas = [f"={index}+1" for index in range(7)]
    path = file_factory("samples.csv", "\n".join(formulas).encode("utf-8"))
    probe = CsvActiveContentProbe()
    if sample_size is not None:
        probe.set_referenced_samples(sample_size)
    result = probe.check(path, "csv")
    limit = 5 if sample_size is None else sample_size
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 7, "sample": ", ".join(formulas[:limit])}


@pytest.mark.parametrize("length", [59, 60, 61])
def test_each_sample_is_limited_to_sixty_characters(file_factory, length):
    formula = "=" + "A" * (length - 1)
    path = file_factory("long.csv", formula.encode("utf-8"))
    result = CsvActiveContentProbe().check(path, "csv")
    assert dict(result.findings[0].detail) == {"count": 1, "sample": formula[:60]}


@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, IsADirectoryError])
def test_read_errors_are_unreadable(monkeypatch, missing_path, error_type):
    def fail(*args, **kwargs):
        raise error_type(errno.EIO, "cannot read CSV")

    monkeypatch.setattr(Path, "read_text", fail)
    result = CsvActiveContentProbe().check(missing_path, "csv")
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": missing_path.name}
    assert dict(result.facts) == {}


def test_missing_csv_is_unreadable(missing_path):
    assert CsvActiveContentProbe().check(missing_path, "csv").codes == (Code.UNREADABLE,)


def test_invalid_utf8_replacement_and_read_parameters(file_factory, monkeypatch):
    path = file_factory("invalid.csv", b"name,value\n\xff,=1+1\n")
    original_read = Path.read_text
    calls = []

    def read_text(target, *args, **kwargs):
        calls.append((target, args, kwargs))
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 1, "sample": "=1+1"}
    assert calls == [(path, (), {"encoding": "utf-8", "errors": "replace"})]


@pytest.mark.parametrize("format_name,normalized", [("pdf", "pdf"), ("html", "html"), ("xlsx", "xlsx"),
                                                   (" .JPG ", "jpeg"), ("txt", "txt"), ("", "")])
def test_mismatched_format_never_reads(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    result = CsvActiveContentProbe().check(missing_path, format_name)
    assert result.codes == (Code.NOT_APPLICABLE,)
    assert dict(result.findings[0].detail) == {"detected_format": normalized}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("password", [None, "", "irrelevant"])
def test_password_is_irrelevant(file_factory, password):
    path = file_factory("password.csv", b"=1+1")
    probe = CsvActiveContentProbe()
    assert probe.check(path, "csv", password) == probe.check(path, "csv")


def test_allowlisting_and_custom_severity_do_not_suppress_raw_formula(file_factory):
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"},
                                 severities={Code.SPREADSHEET_FORMULA: Severity.INFO})
    probe = CsvActiveContentProbe(policy)
    path = file_factory("policy.csv", b"=1+1")
    assert probe.check(path, "csv").codes == (Code.SPREADSHEET_FORMULA,)
    assert probe.policy is policy


def test_formula_payloads_are_never_executed(file_factory, monkeypatch, forbidden_operation):
    path = file_factory("payload.csv", b'=HYPERLINK("https://evil.test/file")')
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    monkeypatch.setattr(webbrowser, "open", forbidden_operation)
    assert CsvActiveContentProbe().check(path, "csv").codes == (Code.SPREADSHEET_FORMULA,)


@pytest.mark.parametrize("bad_path", [None, 1, b"document.csv", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        CsvActiveContentProbe().check(bad_path, "csv")


def test_repeated_calls_and_sample_settings_are_instance_local(file_factory):
    formula = file_factory("dirty.csv", b"=1+1,+SUM(A1:A2)")
    clean = file_factory("safe.csv", b"plain,words")
    probe = CsvActiveContentProbe()
    probe.set_referenced_samples(0)
    assert dict(probe.check(formula, "csv").findings[0].detail) == {"count": 2, "sample": ""}
    assert probe.check(clean, "csv").codes == ()
    assert dict(CsvActiveContentProbe().check(formula, "csv").findings[0].detail) == {
        "count": 2, "sample": "=1+1, +SUM(A1:A2)"}
    assert probe.name == "validation_active_content_csv"
    assert probe.handles == frozenset({"csv"})
