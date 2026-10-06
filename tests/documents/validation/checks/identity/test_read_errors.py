"""Regression contracts for OS failures after the file has been opened."""

import errno
import os

import pytest

from raggae.documents.validation import FileIdentityProbe
from raggae.documents.validation.checks.identity import file as identity_module
from raggae.documents.validation.checks.identity.policy import IdentityFinding as Code


@pytest.mark.parametrize("phase", ["read", "initial-fstat", "final-fstat"])
def test_post_open_os_failure_returns_unreadable_and_closes_stream(
    file_factory, tracked_stream_factory, monkeypatch, phase,
):
    path = file_factory(content=b"known bytes")
    failure = OSError(errno.EIO, "controlled post-open failure")
    stream = tracked_stream_factory(path, read_error=failure if phase == "read" else None)
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    if phase != "read":
        original, descriptor, count = os.fstat, stream.fileno(), 0

        def failing_fstat(fd):
            nonlocal count
            if fd == descriptor:
                count += 1
                if count == (1 if phase == "initial-fstat" else 2):
                    raise failure
            return original(fd)

        monkeypatch.setattr(identity_module.os, "fstat", failing_fstat)

    try:
        outcome = worker.check(path)
    finally:
        assert stream.closed, "The opened descriptor must close even on failure."
    assert outcome.codes == (Code.UNREADABLE,)
    assert "digest" not in outcome.facts
    assert "digest_algorithm" not in outcome.facts


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES, errno.ENOENT, errno.EISDIR, errno.ELOOP, errno.EMLINK])
def test_post_open_os_error_subclasses_are_unreadable_not_open_failure_codes(
    file_factory, tracked_stream_factory, monkeypatch, error_number,
):
    path = file_factory()
    stream = tracked_stream_factory(path, read_error=OSError(error_number, "read failed"))
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert outcome.findings[0].detail["path"] == str(path)
    assert "read failed" in outcome.findings[0].message
    assert stream.closed


def test_failure_after_partial_hash_does_not_publish_a_fingerprint(
    file_factory, tracked_stream_factory, monkeypatch,
):
    from raggae.documents.validation import IdentityPolicy

    path = file_factory(content=b"known bytes")
    stream = tracked_stream_factory(path)
    original_read, calls = stream.read, 0

    def failing_second_read(size):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "read failed after one chunk")
        return original_read(size)

    stream.read = failing_second_read
    worker = FileIdentityProbe(IdentityPolicy(read_chunk_bytes=2))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert calls == 2
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert stream.closed


@pytest.mark.parametrize("error_class", [ValueError, TypeError, RuntimeError])
def test_non_filesystem_errors_are_not_silently_converted_to_findings(
    file_factory, tracked_stream_factory, monkeypatch, error_class,
):
    path = file_factory()
    stream = tracked_stream_factory(path, read_error=error_class("programming error"))
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    with pytest.raises(error_class, match="programming error"):
        worker.check(path)
    assert stream.closed


def test_close_os_error_does_not_return_a_successful_fingerprint(
    file_factory, tracked_stream_factory, monkeypatch,
):
    path = file_factory()
    stream = tracked_stream_factory(path)
    original_close, first_close = stream.close, True

    def failing_close_once():
        nonlocal first_close
        original_close()
        if first_close:
            first_close = False
            raise OSError(errno.EIO, "close failed")

    stream.close = failing_close_once
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert stream.closed
