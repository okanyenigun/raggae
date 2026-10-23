"""Shared resource handling without storage ownership or execution frameworks."""

from dataclasses import dataclass
import os

import pytest

from raggae.documents.normalization import Artifact, SharedInputOutputHandler


@dataclass(frozen=True)
class _Report:
    outcome: str


@pytest.mark.parametrize("disk", [False, True])
def test_handler_closes_streams_and_returns_completed_content(disk, tmp_path):
    streams = []
    destination = tmp_path / "result.txt" if disk else None

    def transform(source, output):
        streams.extend((source, output))
        assert source.tell() == output.tell() == 0
        assert source.read() == b"original"
        output.write(b"normalized")
        if destination is not None:
            assert not destination.exists()
        return _Report("changed")

    report, artifact = SharedInputOutputHandler().execute(
        source=Artifact(data=b"original"), output_path=destination, transform=transform
    )
    assert report.outcome == "changed"
    assert all(stream.closed for stream in streams)
    assert artifact.read_bytes() == b"normalized"
    assert artifact.path == destination
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("outcome", ["failed", "no_change"])
def test_non_changed_output_is_discarded_and_streams_are_closed(disk, outcome, tmp_path):
    streams = []
    original = Artifact(data=b"original")

    def transform(source, output):
        streams.extend((source, output))
        output.write(b"partial output, never publish")
        return _Report(outcome)

    report, artifact = SharedInputOutputHandler().execute(
        source=original,
        output_path=tmp_path / "result.txt" if disk else None,
        transform=transform,
    )
    assert report.outcome == outcome
    assert all(stream.closed for stream in streams)
    assert artifact is (original if outcome == "no_change" else None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("error", [OSError("storage failed"), ValueError("execution failed")])
def test_errors_propagate_after_partial_output_is_removed(disk, error, tmp_path):
    streams = []

    def transform(source, output):
        streams.extend((source, output))
        output.write(b"partial")
        raise error

    with pytest.raises(type(error)) as raised:
        SharedInputOutputHandler().execute(
            source=Artifact(data=b"original"),
            output_path=tmp_path / "result.txt" if disk else None,
            transform=transform,
        )
    assert raised.value is error
    assert all(stream.closed for stream in streams)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("dangling_symlink", [False, True])
def test_existing_destinations_are_preserved_before_transform(tmp_path, dangling_symlink):
    destination = tmp_path / "existing.txt"
    if dangling_symlink:
        destination.symlink_to(tmp_path / "missing.txt")
    else:
        destination.write_bytes(b"user-owned")

    def forbidden(source, output):
        pytest.fail("conflicting destinations must be rejected before transformation")

    with pytest.raises(FileExistsError):
        SharedInputOutputHandler().execute(
            source=Artifact(data=b"original"), output_path=destination, transform=forbidden
        )
    if dangling_symlink:
        assert destination.is_symlink()
    else:
        assert destination.read_bytes() == b"user-owned"
    assert list(tmp_path.iterdir()) == [destination]


def test_destination_created_during_processing_is_not_overwritten(tmp_path):
    destination = tmp_path / "result.txt"

    def transform(source, output):
        output.write(b"our candidate")
        destination.write_bytes(b"another caller's file")
        return _Report("changed")

    with pytest.raises(FileExistsError):
        SharedInputOutputHandler().execute(
            source=Artifact(data=b"original"), output_path=destination, transform=transform
        )
    assert destination.read_bytes() == b"another caller's file"
    assert list(tmp_path.iterdir()) == [destination]


def test_publication_failure_closes_streams_and_removes_staging(tmp_path, monkeypatch):
    error = OSError("publication failed")
    streams = []

    def cannot_publish(*args):
        raise error

    def transform(source, output):
        streams.extend((source, output))
        output.write(b"completed")
        return _Report("changed")

    monkeypatch.setattr("raggae.documents.normalization.io.os.link", cannot_publish)
    with pytest.raises(OSError) as raised:
        SharedInputOutputHandler().execute(
            source=Artifact(data=b"original"),
            output_path=tmp_path / "result.txt", transform=transform,
        )
    assert raised.value is error
    assert all(stream.closed for stream in streams)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("missing_source", [False, True])
def test_missing_paths_do_not_leave_output(missing_source, tmp_path):
    source = Artifact(path=tmp_path / "missing.txt") if missing_source else Artifact(data=b"original")
    destination = tmp_path / "result.txt" if missing_source else tmp_path / "missing" / "result.txt"
    with pytest.raises(FileNotFoundError):
        SharedInputOutputHandler().execute(
            source=source, output_path=destination,
            transform=lambda source, output: _Report("changed"),
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source_kind", ["directory", "fifo"])
def test_source_must_be_a_seekable_regular_file(source_kind, tmp_path):
    path = tmp_path / "not-a-document"
    if source_kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("named pipes unavailable")
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        SharedInputOutputHandler().execute(
            source=Artifact(path=path), output_path=tmp_path / "result.txt",
            transform=lambda source, output: _Report("changed"),
        )
    assert list(tmp_path.iterdir()) == [path]


def test_unknown_outcome_does_not_publish_output(tmp_path):
    with pytest.raises(ValueError, match="unknown outcome"):
        SharedInputOutputHandler().execute(
            source=Artifact(data=b"original"), output_path=tmp_path / "result.txt",
            transform=lambda source, output: _Report("invented"),
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source,output_path,transform", [
    (b"raw", None, lambda source, output: _Report("changed")),
    (Artifact(data=b"raw"), "path", lambda source, output: _Report("changed")),
    (Artifact(data=b"raw"), None, None),
])
def test_invalid_handler_arguments_are_rejected(source, output_path, transform):
    with pytest.raises(TypeError):
        SharedInputOutputHandler().execute(
            source=source, output_path=output_path, transform=transform
        )
