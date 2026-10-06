import codecs
import errno
from pathlib import Path

import pytest

from raggae.documents.validation import CsvActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


ENCODINGS = [("utf-8", codecs.BOM_UTF8, "utf-8-sig"),
             ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16"),
             ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16")]


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
@pytest.mark.parametrize("content", ["", "Résumé,中文 😀\n", '"notes,=1+1",-12.5\n'])
def test_encoded_clean_csv_and_bom_only_are_not_formulas(file_factory, encoding, bom, codec, content):
    data = bom + content.encode(encoding)
    assert data.decode(codec, errors="strict") == content
    path = file_factory("clean.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == ()
    assert dict(result.facts) == {}
    assert path.read_bytes() == data


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
@pytest.mark.parametrize("formula", ["=SUM(1,2)", "+SUM(A1:A2)", "-SUM(A1:A2)", "@SUM(A1:A2)"])
def test_all_prefixes_survive_decoding_and_csv_parsing(file_factory, encoding, bom, codec, formula):
    content = f'"{formula}",-12.5\n'
    data = bom + content.encode(encoding)
    assert data.decode(codec, errors="strict") == content
    path = file_factory("prefix.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 1, "sample": formula}
    assert path.read_bytes() == data


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
def test_bom_selects_expected_text_codec(file_factory, monkeypatch, encoding, bom, codec):
    path = file_factory("codec.csv", bom + "=1+1\n".encode(encoding))
    original_read = Path.read_text
    calls = []

    def read_text(target, *args, **kwargs):
        calls.append((target, args, kwargs))
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert CsvActiveContentProbe().check(path, "csv").codes == (Code.SPREADSHEET_FORMULA,)
    assert calls == [(path, (), {"encoding": codec, "errors": "replace"})]


@pytest.mark.parametrize("phase", ["prefix", "text"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, IsADirectoryError])
def test_errors_during_either_read_are_unreadable(file_factory, monkeypatch, phase, error_type):
    path = file_factory("read-error.csv", codecs.BOM_UTF16_LE + "=1+1".encode("utf-16-le"))
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise error_type(errno.EIO, "cannot read CSV")

    monkeypatch.setattr(Path, "open" if phase == "prefix" else "read_text", fail)
    result = CsvActiveContentProbe().check(path, "csv")
    assert calls == [True]
    assert result.codes == (Code.UNREADABLE,)
    assert dict(result.findings[0].detail) == {"path": path.name}
    assert dict(result.facts) == {}


@pytest.mark.parametrize("format_name", ["pdf", "html", "xlsx", "txt"])
def test_mismatched_format_skips_both_reading_phases(monkeypatch, missing_path, forbidden_operation, format_name):
    monkeypatch.setattr(Path, "open", forbidden_operation)
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    assert CsvActiveContentProbe().check(missing_path, format_name).codes == (Code.NOT_APPLICABLE,)


def test_encoding_selection_does_not_leak_between_calls(file_factory):
    probe = CsvActiveContentProbe()
    for index, (encoding, bom, _) in enumerate(ENCODINGS):
        path = file_factory(f"formula-{index}.csv", bom + "=1+1".encode(encoding))
        assert probe.check(path, "csv").codes == (Code.SPREADSHEET_FORMULA,)
    path = file_factory("unmarked.csv", b"=1+1")
    assert probe.check(path, "csv").codes == (Code.SPREADSHEET_FORMULA,)
    clean = file_factory("plain.csv", b"plain,words")
    assert probe.check(clean, "csv").codes == ()


@pytest.mark.parametrize("encoding,bom,codec", ENCODINGS)
def test_encoding_does_not_change_sample_limits_or_counts(file_factory, encoding, bom, codec):
    formulas = [f"=SUM({index},1)" for index in range(7)]
    content = "\n".join(f'"{formula}"' for formula in formulas)
    data = bom + content.encode(encoding)
    assert data.decode(codec, errors="strict") == content
    path = file_factory("samples.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 7, "sample": ", ".join(formulas[:5])}
    assert path.read_bytes() == data
