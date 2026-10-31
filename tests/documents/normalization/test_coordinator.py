"""Explicit chaining, cumulative artifacts, and final-only publication."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from raggae.documents.normalization import (
    Artifact,
    DocumentNormalizationReport,
    DocumentNormalizer,
    HtmlActiveContentSanitizer,
    HtmlMetadataSanitizer,
    NormalizationRequest,
    NormalizationResult,
    StrictTextEncodingNormalizer,
)
from raggae.documents.normalization import coordinator


class WorkerReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["changed", "no_change", "failed"]
    label: str


class RecordingWorker:
    """A structural worker to test coordination without a document parser."""

    def __init__(
        self,
        name: str = "worker",
        outcome: Literal["changed", "no_change", "failed"] = "changed",
        *,
        suffix: bytes = b"!",
        before: Callable[[NormalizationRequest], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.outcome = outcome
        self.suffix = suffix
        self.before = before
        self.error = error
        self.requests: list[NormalizationRequest] = []
        self.results: list[NormalizationResult[WorkerReport]] = []

    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[WorkerReport]:
        self.requests.append(request)
        if self.before is not None:
            self.before(request)
        if self.error is not None:
            raise self.error
        if self.outcome == "failed":
            artifact = None
        elif self.outcome == "no_change":
            artifact = request.source
        else:
            artifact = Artifact(data=request.source.read_bytes() + self.suffix)
        result = NormalizationResult(
            artifact=artifact,
            report=WorkerReport(outcome=self.outcome, label=self.name),
        )
        self.results.append(result)
        return result


@pytest.mark.parametrize("source_kind", ["bytes", "path", "str", "artifact"])
@pytest.mark.parametrize("to_disk", [False, True])
def test_sources_and_cumulative_changes(source_kind, to_disk, tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"original")
    sources = {
        "bytes": b"original",
        "path": original,
        "str": str(original),
        "artifact": Artifact(data=b"original"),
    }
    destination = tmp_path / "normalized.bin" if to_disk else None
    request = NormalizationRequest(source=sources[source_kind], output_path=destination)
    first = RecordingWorker("first", suffix=b"-first")
    last = RecordingWorker("last", suffix=b"-last")

    result = DocumentNormalizer(workers=(first, last)).normalize(request)

    assert result.outcome == "changed"
    assert isinstance(result.report, DocumentNormalizationReport)
    assert result.artifact is not None
    assert result.artifact.read_bytes() == b"original-first-last"
    assert result.artifact.path == destination
    assert original.read_bytes() == b"original"
    assert first.requests[0].source is request.source
    assert last.requests[0].source is first.results[0].artifact
    assert [step.worker for step in result.report.steps] == ["first", "last"]
    assert result.report.steps[0].report is first.results[0].report
    assert result.report.steps[1].report is last.results[0].report
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_intermediate_requests_preserve_settings_without_exposing_destination(tmp_path):
    destination = tmp_path / "final.bin"

    def check_request(request):
        assert request.output_path is None
        assert request.declared_encoding == "cp1252"
        assert request.target_encoding == "utf-16"
        expected = "secret" if request.source.data == b"original" else None
        assert request.source.password == expected
        assert not destination.exists()
        assert not list(tmp_path.glob(".raggae-*.part"))

    first = RecordingWorker("first", before=check_request)
    second = RecordingWorker("second", before=check_request)
    request = NormalizationRequest(
        source=Artifact(data=b"original", password="secret"),
        output_path=destination,
        declared_encoding="cp1252",
        target_encoding="utf-16",

    )
    result = DocumentNormalizer(workers=(first, second)).normalize(request)

    assert first.requests[0] is not request
    assert second.requests[0] is not request
    assert first.requests[0] is not second.requests[0]
    assert request.source.data == b"original"
    assert request.output_path == destination
    assert request.declared_encoding == "cp1252"
    assert request.target_encoding == "utf-16"
    assert request.source.password == "secret"
    assert destination.read_bytes() == b"original!!"
    assert "secret" not in result.report.model_dump_json()


@pytest.mark.parametrize("outcomes", [
    ("changed", "no_change"),
    ("no_change", "changed"),
    ("changed", "no_change", "changed", "no_change"),
])
@pytest.mark.parametrize("to_disk", [False, True])
def test_any_change_counts_even_when_last_worker_makes_no_change(outcomes, to_disk, tmp_path):
    workers = tuple(RecordingWorker(str(index), outcome) for index, outcome in enumerate(outcomes))
    destination = tmp_path / "final.bin" if to_disk else None
    request = NormalizationRequest(source=b"original", output_path=destination)

    result = DocumentNormalizer(workers=workers).normalize(request)

    assert result.outcome == "changed"
    assert result.artifact is not None
    assert result.artifact.read_bytes() == b"original" + b"!" * outcomes.count("changed")
    assert [step.report.outcome for step in result.report.steps] == list(outcomes)
    if not to_disk:
        assert result.artifact is workers[-1].results[0].artifact


@pytest.mark.parametrize("worker_count", [0, 1, 3])
@pytest.mark.parametrize("destination_kind", ["memory", "new", "existing", "missing_parent", "source"])
def test_no_change_returns_original_and_does_not_touch_destination(worker_count, destination_kind, tmp_path):
    original = tmp_path / "source.bin"
    original.write_bytes(b"original")
    existing = tmp_path / "existing.bin"
    existing.write_bytes(b"leave me alone")
    destinations = {
        "memory": None,
        "new": tmp_path / "new.bin",
        "existing": existing,
        "missing_parent": tmp_path / "missing" / "final.bin",
        "source": original,
    }
    workers = tuple(RecordingWorker(str(index), "no_change") for index in range(worker_count))
    request = NormalizationRequest(source=original, output_path=destinations[destination_kind])

    result = DocumentNormalizer(workers=workers).normalize(request)

    assert result.outcome == "no_change"
    assert result.artifact is request.source
    assert len(result.report.steps) == worker_count
    assert original.read_bytes() == b"original"
    assert existing.read_bytes() == b"leave me alone"
    assert not (tmp_path / "new.bin").exists()
    assert not (tmp_path / "missing").exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_empty_chain_does_not_read_source(tmp_path):
    request = NormalizationRequest(source=tmp_path / "absent.bin", output_path=None)
    result = DocumentNormalizer(workers=()).normalize(request)
    assert result.artifact is request.source
    assert result.outcome == "no_change"
    assert result.report.steps == ()


@pytest.mark.parametrize("failure_index", [0, 1, 2])
@pytest.mark.parametrize("to_disk", [False, True])
def test_failure_stops_chain_and_discards_partial_artifact(failure_index, to_disk, tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"original")
    destination = tmp_path / "final.bin" if to_disk else None
    workers = tuple(
        RecordingWorker(str(index), "failed" if index == failure_index else "changed")
        for index in range(3)
    )

    result = DocumentNormalizer(workers=workers).normalize(
        NormalizationRequest(source=original, output_path=destination)
    )

    assert result.outcome == "failed"
    assert result.artifact is None
    assert [step.worker for step in result.report.steps] == [str(index) for index in range(failure_index + 1)]
    assert result.report.steps[-1].report is workers[failure_index].results[0].report
    assert result.report.steps[-1].report.outcome == "failed"
    assert all(len(worker.requests) == 1 for worker in workers[:failure_index + 1])
    assert all(not worker.requests for worker in workers[failure_index + 1:])
    assert original.read_bytes() == b"original"
    assert not (tmp_path / "final.bin").exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_worker_exceptions_propagate_unchanged_and_halt(error_type, tmp_path):
    error = error_type("worker exception")
    first = RecordingWorker("first")
    failing = RecordingWorker("exception", error=error)
    last = RecordingWorker("must not run")
    destination = tmp_path / "final.bin"

    with pytest.raises(error_type) as caught:
        DocumentNormalizer(workers=(first, failing, last)).normalize(
            NormalizationRequest(source=b"original", output_path=destination)
        )

    assert caught.value is error
    assert not last.requests
    assert not destination.exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_duplicate_worker_names_and_repeated_instance_are_not_collapsed():
    worker = RecordingWorker("same", suffix=b"+")
    result = DocumentNormalizer(workers=(worker, worker)).normalize(
        NormalizationRequest(source=b"original", output_path=None)
    )
    assert result.artifact is not None
    assert result.artifact.data == b"original++"
    assert [step.worker for step in result.report.steps] == ["same", "same"]
    assert len(worker.requests) == 2
    assert worker.requests[1].source is worker.results[0].artifact


def test_chain_state_does_not_leak_between_calls():
    worker = RecordingWorker("change")
    normalizer = DocumentNormalizer(workers=(worker,))
    first = normalizer.normalize(NormalizationRequest(source=b"first", output_path=None))
    second = normalizer.normalize(NormalizationRequest(source=b"second", output_path=None))
    assert first.artifact is not None and first.artifact.data == b"first!"
    assert second.artifact is not None and second.artifact.data == b"second!"
    assert len(first.report.steps) == len(second.report.steps) == 1
    assert worker.requests[1].source.data == b"second"


@pytest.mark.parametrize("destination_kind", ["existing", "source", "dangling_symlink"])
def test_changed_chain_cannot_overwrite_destination(destination_kind, tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"original")
    destination = original if destination_kind == "source" else tmp_path / "final.bin"
    if destination_kind == "existing":
        destination.write_bytes(b"existing")
    elif destination_kind == "dangling_symlink":
        destination.symlink_to(tmp_path / "absent.bin")

    with pytest.raises(FileExistsError):
        DocumentNormalizer(workers=(RecordingWorker(),)).normalize(
            NormalizationRequest(source=original, output_path=destination)
        )

    assert original.read_bytes() == b"original"
    if destination_kind == "existing":
        assert destination.read_bytes() == b"existing"
    elif destination_kind == "dangling_symlink":
        assert destination.is_symlink()
        assert not (tmp_path / "absent.bin").exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_missing_destination_parent_is_not_created(tmp_path):
    destination = tmp_path / "missing" / "final.bin"
    with pytest.raises(FileNotFoundError):
        DocumentNormalizer(workers=(RecordingWorker(),)).normalize(
            NormalizationRequest(source=b"original", output_path=destination)
        )
    assert not destination.parent.exists()


def test_failed_chain_does_not_check_existing_destination(tmp_path):
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"existing")
    result = DocumentNormalizer(workers=(RecordingWorker(outcome="failed"),)).normalize(
        NormalizationRequest(source=b"original", output_path=destination)
    )
    assert result.outcome == "failed"
    assert result.artifact is None
    assert destination.read_bytes() == b"existing"


@pytest.mark.parametrize("payload", [b"", b"x" * (3 * 64 * 1024 + 17)])
def test_publication_handles_empty_and_multichunk_changed_documents(payload, tmp_path):
    worker = RecordingWorker(suffix=b"")
    destination = tmp_path / "final.bin"
    result = DocumentNormalizer(workers=(worker,)).normalize(
        NormalizationRequest(source=payload, output_path=destination)
    )
    assert result.outcome == "changed"
    assert result.artifact is not None and result.artifact.path == destination
    assert destination.read_bytes() == payload


def test_publication_write_exception_discards_staging(monkeypatch, tmp_path):
    error = OSError("disk full")
    destination = tmp_path / "final.bin"

    def fail_after_partial_write(output, data):
        output.write(data[:1])
        raise error

    monkeypatch.setattr(coordinator, "write_all_bytes", fail_after_partial_write)
    with pytest.raises(OSError) as caught:
        DocumentNormalizer(workers=(RecordingWorker(),)).normalize(
            NormalizationRequest(source=b"original", output_path=destination)
        )
    assert caught.value is error
    assert not destination.exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_publication_link_exception_discards_staging(monkeypatch, tmp_path):
    error = OSError("publication failed")
    destination = tmp_path / "final.bin"

    def fail_link(source, target):
        assert Path(source).read_bytes() == b"original!"
        raise error

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError) as caught:
        DocumentNormalizer(workers=(RecordingWorker(),)).normalize(
            NormalizationRequest(source=b"original", output_path=destination)
        )
    assert caught.value is error
    assert not destination.exists()
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_destination_created_during_publication_is_not_overwritten(monkeypatch, tmp_path):
    destination = tmp_path / "final.bin"
    real_link = os.link

    def racing_link(source, target):
        destination.write_bytes(b"someone else's file")
        return real_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(FileExistsError):
        DocumentNormalizer(workers=(RecordingWorker(),)).normalize(
            NormalizationRequest(source=b"original", output_path=destination)
        )
    assert destination.read_bytes() == b"someone else's file"
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("invalid_request", [None, {}, b"source", "source.bin"])
def test_wrong_request_type_is_rejected_before_workers_run(invalid_request):
    worker = RecordingWorker()
    with pytest.raises(TypeError, match="NormalizationRequest"):
        DocumentNormalizer(workers=(worker,)).normalize(invalid_request)
    assert not worker.requests


@pytest.mark.parametrize("to_disk", [False, True])
def test_real_html_workers_chain_encoding_sanitization_and_metadata(to_disk, tmp_path):
    html = '<html><head><meta name="author" content="Alice"></head><body><script>alert(1)</script><p>Café</p></body></html>'
    original = tmp_path / "original.html"
    original.write_bytes(html.encode("cp1252"))
    destination = tmp_path / "normalized.html" if to_disk else None
    result = DocumentNormalizer(
        workers=(
            StrictTextEncodingNormalizer(),
            HtmlActiveContentSanitizer(),
            HtmlMetadataSanitizer(),
        )
    ).normalize(
        NormalizationRequest(source=original, output_path=destination, declared_encoding="cp1252")
    )

    assert result.outcome == "changed"
    assert result.artifact is not None
    normalized = result.artifact.read_bytes().decode("utf-8")
    assert "<p>Café</p>" in normalized
    assert "script" not in normalized
    assert "author" not in normalized
    assert "Alice" not in normalized
    assert original.read_bytes() == html.encode("cp1252")
    assert [step.report.outcome for step in result.report.steps] == ["changed"] * 3
    data = result.report.model_dump(mode="json")
    assert data["steps"][0]["report"]["target_encoding"] == "utf-8"
    assert data["steps"][1]["report"]["removals"]
    assert data["steps"][2]["report"]["removed_tags"] == 1


def test_real_encoding_change_is_published_after_metadata_no_change(tmp_path):
    destination = tmp_path / "final.html"
    result = DocumentNormalizer(
        workers=(StrictTextEncodingNormalizer(), HtmlMetadataSanitizer())
    ).normalize(
        NormalizationRequest(
            source="<p>Café</p>".encode("cp1252"),
            output_path=destination,
            declared_encoding="cp1252",
        )
    )
    assert result.outcome == "changed"
    assert [step.report.outcome for step in result.report.steps] == ["changed", "no_change"]
    assert destination.read_bytes() == "<p>Café</p>".encode("utf-8")


def test_real_encoding_failure_prevents_html_worker_and_publication(tmp_path):
    last = RecordingWorker("must not run")
    destination = tmp_path / "final.html"
    result = DocumentNormalizer(
        workers=(StrictTextEncodingNormalizer(), last)
    ).normalize(NormalizationRequest(source=b"\xffinvalid", output_path=destination))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert len(result.report.steps) == 1
    assert result.report.steps[0].report.issues
    assert not last.requests
    assert not destination.exists()
