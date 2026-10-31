"""Declared routes, native-tool failures, and shared request/result storage."""

import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationWorker,
    DocumentToHtmlFinding,
    DocumentToHtmlIssue,
    DocumentToHtmlReport,
    NormalizationRequest,
    NormalizationResult,
    DocumentToHtmlConverter,
)
from raggae.documents.normalization.format_conversion import pandoc

SOURCE = b"# Report\n\nAn example document.\n"
HTML = b"<!DOCTYPE html><html><head></head><body><h1>Report</h1><p>An example document.</p></body></html>"


def native_paths(args):
    return Path(args[-1]), Path(next(value.removeprefix("--output=") for value in args if value.startswith("--output=")))


@pytest.fixture
def fake_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(pandoc.shutil, "which", lambda value: "/fake tools/pandoc")

    def run(args, **kwargs):
        source, output = native_paths(args)
        calls.append((args, kwargs, source.read_bytes()))
        output.write_bytes(HTML)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pandoc.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("source_format", ["docx", "odt", "rtf", "epub", "markdown", "md"])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_declared_routes_share_input_output_contract(source_format, source_kind, disk, fake_engine, tmp_path):
    source = SOURCE
    if source_kind == "path":
        source = tmp_path / "name; not-its-declared-format.bin"
        source.write_bytes(SOURCE)
    elif source_kind == "artifact":
        source = Artifact(data=SOURCE)
    destination = tmp_path / "result.html" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    worker = DocumentToHtmlConverter(source_format, timeout_seconds=17)
    result = worker.normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == HTML
    assert result.artifact.path == destination
    assert request.source.read_bytes() == SOURCE
    assert result.report.output_size_bytes == len(HTML)
    assert result.report.source_format == ("markdown" if source_format == "md" else source_format)
    assert result.report.issues == ()
    assert DocumentToHtmlReport.model_validate_json(result.report.model_dump_json()) == result.report
    args, options, staged_bytes = fake_engine[0]
    assert staged_bytes == SOURCE
    staged, candidate = native_paths(args)
    assert staged.name == f"source.{worker.source_format}"
    assert candidate.parent == staged.parent == options["cwd"]
    assert args[0] == "/fake tools/pandoc"
    assert "--sandbox" in args
    assert f"--from={worker.source_format}" in args
    assert "--to=html5" in args
    assert "--standalone" in args
    assert options["shell"] is False
    assert options["check"] is False
    assert options["timeout"] == 17
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert not staged.parent.exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_missing_dependency_safe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(pandoc.shutil, "which", lambda value: None)

    def forbidden(*args, **kwargs):
        pytest.fail("missing dependency must not start a process")

    monkeypatch.setattr(pandoc.subprocess, "run", forbidden)
    result = DocumentToHtmlConverter().normalize(NormalizationRequest(source=SOURCE, output_path=tmp_path / "result.html"))
    assert result.artifact is None
    assert result.outcome == "failed"
    assert result.report.source_format == "docx"
    assert result.report.output_size_bytes is None
    assert result.report.issues[0].code == DocumentToHtmlFinding.DEPENDENCY_UNAVAILABLE
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["timeout", "start", "nonzero", "missing", "empty", "whitespace", "encoding", "symlink"])
def test_native_failures_discard_output_and_cleanup(failure, fake_engine, monkeypatch, tmp_path):
    scratch = []
    private = tmp_path / "private-diagnostic"
    private.write_bytes(b"private")

    def run(args, **kwargs):
        source, output = native_paths(args)
        scratch.append(source.parent)
        if failure == "timeout":
            output.write_bytes(b"partial")
            raise subprocess.TimeoutExpired("private diagnostic", 1, stderr=b"private source text")
        if failure == "start":
            raise OSError("private diagnostic")
        if failure == "nonzero":
            output.write_bytes(HTML)
            return subprocess.CompletedProcess(args, 42)
        if failure == "empty":
            output.write_bytes(b"")
        elif failure == "whitespace":
            output.write_bytes(b" \n\t")
        elif failure == "encoding":
            output.write_bytes(b"\xff")
        elif failure == "symlink":
            output.symlink_to(private)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pandoc.subprocess, "run", run)
    result = DocumentToHtmlConverter("md").normalize(NormalizationRequest(source=SOURCE, output_path=tmp_path / "result.html"))
    assert result.artifact is None
    expected = {
        "timeout": DocumentToHtmlFinding.TIMEOUT,
        "start": DocumentToHtmlFinding.DEPENDENCY_UNAVAILABLE,
        "nonzero": DocumentToHtmlFinding.CONVERSION_FAILED,
    }.get(failure, DocumentToHtmlFinding.INVALID_OUTPUT)
    assert result.report.issues[0].code == expected
    assert "private" not in result.report.model_dump_json()
    assert result.report.output_size_bytes is None
    assert all(not directory.exists() for directory in scratch)
    assert not (tmp_path / "result.html").exists()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert private.read_bytes() == b"private"


def test_relative_executable_resolved_before_changing_directory(fake_engine, monkeypatch):
    captured = []

    def which(value):
        captured.append(value)
        return "./tools/pandoc"

    monkeypatch.setattr(pandoc.shutil, "which", which)
    result = DocumentToHtmlConverter("md", executable=Path("tools/pandoc")).normalize(NormalizationRequest(source=SOURCE, output_path=None))
    assert result.outcome == "changed"
    assert captured == ["tools/pandoc"]
    assert Path(fake_engine[0][0][0]).is_absolute()
    assert fake_engine[0][0][0] == str(Path("tools/pandoc").absolute())


@pytest.mark.parametrize("write_result", [None, 0, -1, "error"])
def test_destination_errors_propagate_after_native_cleanup(write_result, fake_engine):
    class BrokenOutput:
        def write(self, data):
            if write_result == "error":
                raise OSError("destination error")
            return write_result

    with BytesIO(SOURCE) as source, pytest.raises(OSError):
        DocumentToHtmlConverter("md")._transform(source, BrokenOutput())
    assert not Path(fake_engine[0][1]["cwd"]).exists()


def test_partial_writes_complete_html(fake_engine):
    class PartialOutput(BytesIO):
        def write(self, data):
            return super().write(data[:3])

    with BytesIO(SOURCE) as source, PartialOutput() as output:
        report = DocumentToHtmlConverter("md")._transform(source, output)
        assert report.output_size_bytes == len(HTML)
        assert output.getvalue() == HTML


def test_source_read_error_cleans_native_scratch(fake_engine, monkeypatch):
    from tempfile import TemporaryDirectory

    scratch = []

    def directory(**kwargs):
        result = TemporaryDirectory(**kwargs)
        scratch.append(Path(result.name))
        return result

    class BrokenSource(BytesIO):
        def read(self, *args):
            raise OSError("source error")

    monkeypatch.setattr(pandoc, "TemporaryDirectory", directory)
    with BrokenSource(SOURCE) as source, BytesIO() as output, pytest.raises(OSError, match="source error"):
        DocumentToHtmlConverter("md")._transform(source, output)
    assert fake_engine == []
    assert all(not path.exists() for path in scratch)


def test_publish_error_cleans_both_scratch_areas(fake_engine, monkeypatch, tmp_path):
    from raggae.documents.normalization import io

    def broken_link(*args):
        raise OSError("publish error")

    monkeypatch.setattr(io.os, "link", broken_link)
    with pytest.raises(OSError, match="publish error"):
        DocumentToHtmlConverter("md").normalize(NormalizationRequest(source=SOURCE, output_path=tmp_path / "result.html"))
    assert not Path(fake_engine[0][1]["cwd"]).exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("same_source", [False, True])
def test_existing_destination_not_overwritten(same_source, fake_engine, tmp_path):
    source = tmp_path / "original.md"
    source.write_bytes(SOURCE)
    destination = source if same_source else tmp_path / "result.html"
    if not same_source:
        destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        DocumentToHtmlConverter("md").normalize(NormalizationRequest(source=source, output_path=destination))
    assert source.read_bytes() == SOURCE
    assert destination.read_bytes() == (SOURCE if same_source else b"existing")
    assert fake_engine == []
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_repeated_calls_do_not_share_scratch(fake_engine):
    worker = DocumentToHtmlConverter("md")
    for _ in range(2):
        result = worker.normalize(NormalizationRequest(source=SOURCE, output_path=None))
        assert result.artifact.read_bytes() == HTML
    assert fake_engine[0][1]["cwd"] != fake_engine[1][1]["cwd"]
    assert all(not call[1]["cwd"].exists() for call in fake_engine)


def test_contract_and_unused_settings(fake_engine):
    worker = DocumentToHtmlConverter("md")
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_document_to_html"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})
    result = worker.normalize(NormalizationRequest(source=Artifact(data=SOURCE, password="unused-secret"), output_path=None,  declared_encoding="ascii"))
    assert result.outcome == "changed"
    assert "unused-secret" not in repr(result)


@pytest.mark.parametrize("value,expected", [("DOCX", "docx"), (" .ODT ", "odt"), (".MD", "markdown"), (" Markdown ", "markdown")])
def test_source_format_aliases(value, expected):
    assert DocumentToHtmlConverter(value).source_format == expected


@pytest.mark.parametrize("value", [None, False, 1, b"docx"])
def test_source_format_type_errors(value):
    with pytest.raises(TypeError):
        DocumentToHtmlConverter(value)


@pytest.mark.parametrize("value", ["", " ", "html", "pdf", "xlsx", "--lua-filter=evil", "docx+raw_html"])
def test_unsupported_routes_rejected(value):
    with pytest.raises(ValueError):
        DocumentToHtmlConverter(value)


@pytest.mark.parametrize("value", [False, True, None, 1.0, "120"])
def test_timeout_type_errors(value):
    with pytest.raises(TypeError):
        DocumentToHtmlConverter(timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1])
def test_timeout_must_be_positive(value):
    with pytest.raises(ValueError):
        DocumentToHtmlConverter(timeout_seconds=value)


@pytest.mark.parametrize("value", [False, 1, b"pandoc"])
def test_executable_type_errors(value):
    with pytest.raises(TypeError):
        DocumentToHtmlConverter(executable=value)


@pytest.mark.parametrize("value", ["", " "])
def test_executable_blank_rejected(value):
    with pytest.raises(ValueError):
        DocumentToHtmlConverter(executable=value)


def test_constructor_does_not_probe_or_run_native_tools(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native discovery and execution belong to normalize, not construction")

    monkeypatch.setattr(pandoc.shutil, "which", forbidden)
    monkeypatch.setattr(pandoc.subprocess, "run", forbidden)
    assert DocumentToHtmlConverter().source_format == "docx"


def test_report_frozen_and_serializable():
    report = DocumentToHtmlReport(outcome="changed", source_format="markdown", output_size_bytes=len(HTML))
    assert DocumentToHtmlReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.output_size_bytes = 1
    with pytest.raises(ValidationError):
        DocumentToHtmlIssue(code=DocumentToHtmlFinding.TIMEOUT, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "changed"}, {"outcome": "no_change"},
    {"outcome": "changed", "output_size_bytes": 0},
    {"outcome": "changed", "output_size_bytes": True},
    {"outcome": "changed", "output_size_bytes": "100"},
    {"outcome": "changed", "output_size_bytes": 1, "issues": [{"code": DocumentToHtmlFinding.TIMEOUT, "message": "Timeout"}]},
    {"outcome": "failed", "output_size_bytes": 1, "issues": [{"code": DocumentToHtmlFinding.TIMEOUT, "message": "Timeout"}]},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        DocumentToHtmlReport(source_format="markdown", **kwargs)


@pytest.fixture
def real_pandoc():
    executable = shutil.which("pandoc")
    if executable is None:
        pytest.skip("optional Pandoc executable is not installed")
    return executable


@pytest.mark.parametrize("source_format", ["docx", "odt", "rtf", "epub", "markdown"])
def test_real_pandoc_all_routes(source_format, real_pandoc, tmp_path):
    source = SOURCE
    if source_format != "markdown":
        path = tmp_path / f"fixture.{source_format}"
        subprocess.run(
            (real_pandoc, "--from=markdown", f"--to={source_format}", f"--output={path}"),
            input=SOURCE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=30, check=True,
        )
        source = path.read_bytes()
    result = DocumentToHtmlConverter(source_format, executable=real_pandoc).normalize(
        NormalizationRequest(source=source, output_path=None)
    )
    assert result.outcome == "changed", result.report
    html = result.artifact.read_bytes().decode("utf-8")
    assert "<!DOCTYPE html>" in html
    assert "<html" in html
    assert "Report" in html
    assert "An example document." in html
    assert result.report.output_size_bytes == len(result.artifact.read_bytes())


def test_real_pandoc_unicode_tables_and_raw_html(real_pandoc):
    source = '# Caf\u00e9\n\n| Item | Price |\n|---|---|\n| Th\u00e9 | 3 |\n\n<script>alert("unsafe")</script>\n'.encode("utf-8")
    result = DocumentToHtmlConverter("md", executable=real_pandoc).normalize(
        NormalizationRequest(source=source, output_path=None)
    )
    assert result.outcome == "changed"
    html = result.artifact.read_bytes().decode("utf-8")
    assert "Caf\u00e9" in html and "Th\u00e9" in html
    assert "<table>" in html
    assert '<script>alert("unsafe")</script>' in html


def test_real_pandoc_malformed_docx_rejected(real_pandoc, tmp_path):
    result = DocumentToHtmlConverter("docx", executable=real_pandoc).normalize(
        NormalizationRequest(source=b"not a DOCX archive", output_path=tmp_path / "result.html")
    )
    assert result.artifact is None
    assert result.report.issues[0].code == DocumentToHtmlFinding.CONVERSION_FAILED
    assert not list(tmp_path.iterdir())
