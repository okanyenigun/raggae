"""CSV cell values, numeric exceptions, no-ops, malformed input, and cleanup."""

import codecs
import csv
from io import BytesIO, StringIO

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    CsvFormulaFinding,
    CsvFormulaIssue,
    NormalizationWorker,
    CsvFormulaReport,
    CsvFormulaSanitizer,
    NormalizationRequest,
    NormalizationResult,
    StrictTextEncodingNormalizer,
)


def encode_rows(rows):
    with StringIO(newline="") as output:
        csv.writer(output).writerows(rows)
        return output.getvalue().encode("utf-8")


def decode_rows(data):
    with StringIO(data.decode("utf-8-sig"), newline="") as source:
        return list(csv.reader(source))


@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_all_formula_prefixes_and_preserved_ordinary_cells(source_kind, disk, tmp_path):
    rows = [
        ["name", "value", "notes"],
        ["Café", '=WEBSERVICE("https://example.invalid")', "Keep, ordinary text"],
        ["+SUM(A1:A2)", "-SUM(A1:A2)", "@SUM(A1:A2)"],
        ["-500", "+500", "-1.2e+3"],
        ["", "line one\r\nline two", "quote: \""],
    ]
    original = encode_rows(rows)
    source = original
    if source_kind == "path":
        path = tmp_path / "source.bin"
        path.write_bytes(original)
        source = path
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.csv" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = CsvFormulaSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.changed_cells == 4
    assert result.report.issues == ()
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    expected = [row[:] for row in rows]
    expected[1][1] = "'" + expected[1][1]
    expected[2] = ["'" + cell for cell in expected[2]]
    assert decode_rows(result.artifact.read_bytes()) == expected
    assert "example.invalid" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert CsvFormulaReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("cell", [
    "=1+1", "+SUM(A1:A2)", "-SUM(A1:A2)", "@SUM(A1:A2)",
    "=", "+", "-", "@", "+NaN", "-Infinity", "+1_000", "-12 trailing",
])
def test_formula_like_cells_prefixed(cell):
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=encode_rows([[cell]]), output_path=None))
    assert result.outcome == "changed"
    assert result.report.changed_cells == 1
    assert decode_rows(result.artifact.data) == [["'" + cell]]


@pytest.mark.parametrize("cell", [
    "-500", "+500", "-0", "+0", "-12.5", "+.5", "-1.",
    "-1.2e+3", "+2E-4", "", "123", "text", "'=1+1",
])
def test_ordinary_values_and_signed_numbers_are_exact_noops(cell, tmp_path):
    original = Artifact(data=encode_rows([[cell]]))
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=original, output_path=tmp_path / "unused.csv"))
    assert result.outcome == "no_change"
    assert result.report.changed_cells == 0
    assert result.artifact is original
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("data", [b"", b"\r\n", b"name,value\r\nAlice,12\r\n", codecs.BOM_UTF8 + b"plain,12\r\n"])
def test_original_bytes_preserved_when_no_cell_changes(data):
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=data, output_path=None))
    assert result.outcome == "no_change"
    assert result.artifact.data == data


def test_utf8_bom_does_not_hide_first_cell_formula():
    data = codecs.BOM_UTF8 + b"=1+1,ordinary\r\n"
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=data, output_path=None))
    assert result.report.changed_cells == 1
    assert result.artifact.data == codecs.BOM_UTF8 + b"'=1+1,ordinary\n"


def test_blank_rows_unequal_row_lengths_and_final_newline():
    original = b"a,b\r\n\r\n=1\r\nx,y,z"
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=original, output_path=None))
    assert decode_rows(result.artifact.data) == [["a", "b"], [], ["'=1"], ["x", "y", "z"]]
    assert result.artifact.data == b"a,b\n\n'=1\nx,y,z\n"


def test_header_cells_are_not_exempt_and_already_escaped_cells_stay_escaped():
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(
        source=encode_rows([["=header", "'=escaped"], ["=body", "ordinary"]]), output_path=None,
    ))
    assert result.report.changed_cells == 2
    assert decode_rows(result.artifact.data) == [["'=header", "'=escaped"], ["'=body", "ordinary"]]


def test_formula_characters_inside_cell_not_changed():
    source = encode_rows([["ordinary =1+1", "person@example.invalid", "a-b"]])
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.outcome == "no_change"
    assert result.artifact.data == source


def test_whitespace_not_trimmed_and_dialect_not_guessed():
    # These limitations are explicit: first-character matching, comma dialect only.
    source = b"name;=formula\n name, =formula\n"
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.outcome == "no_change"
    assert result.artifact.data == source


@pytest.mark.parametrize("data", [b"\xff", b"name,caf\xe9\n", b"=1+1,\xc3", b'"unclosed', b'a,"unclosed', b'"value"junk,next'])
def test_invalid_utf8_or_malformed_quoting_failed(data, tmp_path):
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output.csv"))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.changed_cells == 0
    assert result.report.issues[0].code == CsvFormulaFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_legacy_encoding_can_be_prepared_by_text_worker():
    prepared = StrictTextEncodingNormalizer().normalize(NormalizationRequest(
        source=b"caf\xe9,=1+1\n", output_path=None, declared_encoding="cp1252",
    ))
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=prepared.artifact, output_path=None))
    assert decode_rows(result.artifact.data) == [["café", "'=1+1"]]


def test_idempotent_and_shared_settings_ignored():
    worker = CsvFormulaSanitizer()
    first = worker.normalize(NormalizationRequest(
        source=b"name,=1+1\n", output_path=None, declared_encoding="ascii", password="unused-secret",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


def test_encoder_failure_cleans_output(monkeypatch, tmp_path):
    def broken_writer(*args, **kwargs):
        raise csv.Error("private encoder diagnostics")

    monkeypatch.setattr(csv, "writer", broken_writer)
    result = CsvFormulaSanitizer().normalize(NormalizationRequest(source=b"=1+1\n", output_path=tmp_path / "output.csv"))
    assert result.artifact is None
    assert result.report.issues[0].code == CsvFormulaFinding.ENCODE_FAILED
    assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_partial_output_failure_propagates_and_is_cleaned(monkeypatch, tmp_path):
    worker = CsvFormulaSanitizer()
    transform = worker._transform
    error = OSError("disk full")

    class BrokenOutput:
        def __init__(self, output):
            self.output = output

        def write(self, data):
            self.output.write(data[:2])
            raise error

    def wrapped(source, output):
        return transform(source, BrokenOutput(output))

    monkeypatch.setattr(worker, "_transform", wrapped)
    with pytest.raises(OSError) as caught:
        worker.normalize(NormalizationRequest(source=b"=1+1\n", output_path=tmp_path / "output.csv"))
    assert caught.value is error
    assert not list(tmp_path.iterdir())


def test_short_writes_supported():
    class ShortOutput(BytesIO):
        def write(self, data):
            return super().write(data[:2])

    with BytesIO(b"=1+1\n") as source, ShortOutput() as output:
        report = CsvFormulaSanitizer()._transform(source, output)
        assert report.changed_cells == 1
        assert output.getvalue() == b"'=1+1\n"


@pytest.mark.parametrize("count", [None, 0, -1, True, "1", 999])
def test_invalid_write_counts_raise(count):
    class InvalidOutput:
        def write(self, data):
            return count

    with BytesIO(b"=1+1\n") as source:
        with pytest.raises(OSError, match="valid number of bytes"):
            CsvFormulaSanitizer()._transform(source, InvalidOutput())


def test_existing_destination_not_overwritten(tmp_path):
    path = tmp_path / "existing.csv"
    original = b"=1+1\n"
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        CsvFormulaSanitizer().normalize(NormalizationRequest(source=path, output_path=path))
    assert path.read_bytes() == original


def test_worker_contract():
    worker = CsvFormulaSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_csv_formula"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


@pytest.mark.parametrize("count", [-1, True, "1"])
def test_invalid_report_count_rejected(count):
    with pytest.raises(ValidationError):
        CsvFormulaReport(outcome="changed", changed_cells=count)


def test_report_read_only_and_json_roundtrip():
    report = CsvFormulaReport(outcome="changed", changed_cells=2)
    assert CsvFormulaReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.changed_cells = 3
    with pytest.raises(ValidationError):
        CsvFormulaIssue(code=CsvFormulaFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "changed"}, {"outcome": "failed"}, {"outcome": "no_change", "changed_cells": 1},
    {"outcome": "failed", "changed_cells": 1, "issues": [CsvFormulaIssue(code=CsvFormulaFinding.DECODE_FAILED, message="Failed.")]},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        CsvFormulaReport(**kwargs)
