"""Real file behavior and deterministic filesystem/identity races."""

import errno
import hashlib
from math import ceil
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from raggae.documents.validation import FileIdentityProbe, IdentityPolicy
from raggae.documents.validation.checks.identity import file as identity_module
from raggae.documents.validation.checks.identity.policy import IdentityFinding as Code


def snapshot(status, **changes):
    fields = {name: getattr(status, name) for name in (
        "st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns",
    )}
    return SimpleNamespace(**{**fields, **changes})


def descriptor_statuses(monkeypatch, stream, statuses):
    """Override only this descriptor, leaving unrelated OS work untouched."""
    descriptor = stream.fileno()
    original = os.fstat
    sequence = iter(statuses)

    def controlled_fstat(fd):
        return next(sequence) if fd == descriptor else original(fd)

    monkeypatch.setattr(identity_module.os, "fstat", controlled_fstat)


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b", "blake2s"])
@pytest.mark.parametrize("as_string", [False, True])
def test_real_file_has_expected_identity_and_digest(file_factory, algorithm, as_string):
    data = b"known bytes\x00\xff"
    path = file_factory("résumé with spaces.txt", data)
    status = path.stat()
    policy = IdentityPolicy(hash_algorithm=algorithm)
    worker = FileIdentityProbe(policy)
    outcome = worker.check(str(path) if as_string else path)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {
        "size_bytes": len(data), "device": status.st_dev, "inode": status.st_ino,
        "mtime_ns": status.st_mtime_ns, "digest": hashlib.new(algorithm, data).hexdigest(),
        "digest_algorithm": algorithm,
    }
    assert worker.policy is policy
    assert worker.name == "validator_identity_file"
    assert path.read_bytes() == data


@pytest.mark.parametrize("size,code", [(2, Code.TOO_SMALL), (3, None), (4, None), (9, None), (10, None), (11, Code.TOO_LARGE)])
@pytest.mark.parametrize("digest", [False, True])
def test_minimum_and_maximum_size_boundaries(file_factory, size, code, digest):
    path = file_factory(content=b"x" * size)
    outcome = FileIdentityProbe(IdentityPolicy(min_bytes=3, max_bytes=10, compute_digest=digest)).check(path)
    assert outcome.codes == ((code,) if code is not None else ())
    assert outcome.facts["size_bytes"] == size
    assert ("digest" in outcome.facts) is (digest and code != Code.TOO_LARGE)
    if code is not None:
        assert dict(outcome.findings[0].detail) == {"limit": 3 if code == Code.TOO_SMALL else 10, "observed": size}


@pytest.mark.parametrize("minimum", [0, 1])
def test_empty_file_default_rejection_and_explicit_allowance(file_factory, minimum):
    path = file_factory(content=b"")
    outcome = FileIdentityProbe(IdentityPolicy(min_bytes=minimum)).check(path)
    assert outcome.codes == ((Code.TOO_SMALL,) if minimum else ())
    assert outcome.facts["digest"] == hashlib.sha256(b"").hexdigest()
    assert outcome.facts["size_bytes"] == 0


@pytest.mark.parametrize("chunk_size", [1, 5, 100])
def test_digest_is_complete_chunked_and_closes_the_stream(
    file_factory, tracked_stream_factory, monkeypatch, chunk_size,
):
    path = file_factory(content=b"abcde")
    stream = tracked_stream_factory(path)
    worker = FileIdentityProbe(IdentityPolicy(read_chunk_bytes=chunk_size))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == ()
    assert outcome.facts["digest"] == hashlib.sha256(b"abcde").hexdigest()
    assert stream.read_sizes == [chunk_size] * (ceil(5 / chunk_size) + 1)
    assert stream.closed


@pytest.mark.parametrize("too_large", [False, True])
def test_disabled_digest_and_oversize_do_not_read_content(
    file_factory, tracked_stream_factory, monkeypatch, forbidden_operation, too_large,
):
    path = file_factory(content=b"abcde")
    stream = tracked_stream_factory(path)
    stream.read = forbidden_operation
    worker = FileIdentityProbe(IdentityPolicy(max_bytes=4 if too_large else 10, compute_digest=too_large))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    monkeypatch.setattr(worker, "_digest", forbidden_operation)
    outcome = worker.check(path)
    assert outcome.codes == ((Code.TOO_LARGE,) if too_large else ())
    assert "digest" not in outcome.facts
    assert "digest_algorithm" not in outcome.facts
    assert outcome.facts["size_bytes"] == 5
    assert stream.closed


def test_missing_file_is_reported_before_open(missing_path, monkeypatch, forbidden_operation):
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    outcome = worker.check(missing_path)
    assert outcome.codes == (Code.NOT_FOUND,)
    assert dict(outcome.facts) == {}
    assert outcome.findings[0].detail["path"] == str(missing_path)


def test_directory_is_reported_before_open(tmp_path, monkeypatch, forbidden_operation):
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    outcome = worker.check(tmp_path)
    assert outcome.codes == (Code.NOT_A_REGULAR_FILE,)
    assert dict(outcome.findings[0].detail) == {"kind": "directory"}
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("value", [None, b"file", 7, object()])
def test_invalid_path_types_raise_type_error(value):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        FileIdentityProbe().check(value)


ERROR_CASES = [
    (errno.ENOENT, Code.NOT_FOUND), (errno.EISDIR, Code.NOT_A_REGULAR_FILE),
    (errno.EACCES, Code.UNREADABLE), (errno.ELOOP, Code.SYMLINK),
    (errno.EMLINK, Code.SYMLINK), (errno.EIO, Code.UNREADABLE),
]


@pytest.mark.parametrize("phase", ["lstat", "stat", "open"])
@pytest.mark.parametrize("error_number,code", ERROR_CASES)
def test_filesystem_failures_are_translated_at_each_pre_read_stage(
    file_factory, monkeypatch, phase, error_number, code,
):
    path = file_factory()
    worker = FileIdentityProbe()
    failure = OSError(error_number, "controlled filesystem failure")
    if phase == "open":
        def fail_open(target):
            raise failure
        monkeypatch.setattr(worker, "_open", fail_open)
    else:
        original = getattr(Path, phase)

        def fail_path(target, *args, **kwargs):
            # Path.lstat delegates to stat(follow_symlinks=False); keep the
            # lstat stage intact when the failure belongs to the later stat.
            if target == path and (phase == "lstat" or kwargs.get("follow_symlinks", True)):
                raise failure
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, phase, fail_path)
    outcome = worker.check(path)
    assert outcome.codes == (code,)
    assert dict(outcome.facts) == {}
    if code == Code.NOT_A_REGULAR_FILE:
        assert outcome.findings[0].detail["kind"] == "directory"
    else:
        assert outcome.findings[0].detail["path"] == str(path)


def make_symlink(link, target):
    try:
        link.symlink_to(target)
    except NotImplementedError:
        pytest.skip("This platform does not implement symlinks.")
    except OSError as error:
        if error.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP}:
            pytest.skip(f"Symlinks are unavailable in this environment: {error}.")
        raise


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_symlink_policy_and_target_kind(file_factory, tmp_path, allow, target_kind):
    target = file_factory(content=b"target bytes") if target_kind == "file" else tmp_path / target_kind
    if target_kind == "directory":
        target.mkdir()
    link = tmp_path / "document-link.txt"
    make_symlink(link, target)
    outcome = FileIdentityProbe(IdentityPolicy(allow_symlinks=allow)).check(link)
    if not allow:
        assert outcome.codes == (Code.SYMLINK,)
    elif target_kind == "directory":
        assert outcome.codes == (Code.NOT_A_REGULAR_FILE,)
    elif target_kind == "missing":
        assert outcome.codes == (Code.NOT_FOUND,)
    else:
        assert outcome.codes == ()
        assert outcome.facts["digest"] == hashlib.sha256(b"target bytes").hexdigest()
        assert outcome.facts["inode"] == target.stat().st_ino
        assert target.read_bytes() == b"target bytes"
    assert link.is_symlink()


def test_disallowed_symlink_is_never_followed_or_opened(file_factory, tmp_path, monkeypatch, forbidden_operation):
    target = file_factory()
    link = tmp_path / "link.txt"
    make_symlink(link, target)
    original_stat = Path.stat

    def guarded_stat(path, *args, **kwargs):
        if path == link and kwargs.get("follow_symlinks", True):
            forbidden_operation()
        return original_stat(path, *args, **kwargs)

    worker = FileIdentityProbe()
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    assert worker.check(link).codes == (Code.SYMLINK,)


@pytest.mark.parametrize("mode,kind", [
    (stat.S_IFIFO, "fifo"), (stat.S_IFSOCK, "socket"),
    (stat.S_IFCHR, "character device"), (stat.S_IFBLK, "block device"),
    (0, "unknown"),
])
def test_non_regular_file_modes_are_refused_without_opening(
    file_factory, monkeypatch, forbidden_operation, mode, kind,
):
    path = file_factory()
    status, original_stat = path.stat(), Path.stat

    def controlled_stat(target, *args, **kwargs):
        if target == path and kwargs.get("follow_symlinks", True):
            return snapshot(status, st_mode=mode)
        return original_stat(target, *args, **kwargs)

    worker = FileIdentityProbe()
    monkeypatch.setattr(Path, "stat", controlled_stat)
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    outcome = worker.check(path)
    assert outcome.codes == (Code.NOT_A_REGULAR_FILE,)
    assert dict(outcome.findings[0].detail) == {"kind": kind}
    assert dict(outcome.facts) == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Named pipes require os.mkfifo.")
def test_real_fifo_is_rejected_without_opening(tmp_path, monkeypatch, forbidden_operation):
    path = tmp_path / "document.pipe"
    os.mkfifo(path)
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    outcome = worker.check(path)
    assert outcome.codes == (Code.NOT_A_REGULAR_FILE,)
    assert outcome.findings[0].detail["kind"] == "fifo"


@pytest.mark.parametrize("allow_symlinks", [False, True])
def test_open_requests_platform_safety_flags(file_factory, monkeypatch, allow_symlinks):
    path = file_factory()
    original_open, calls = os.open, []

    def recording_open(target, flags, *args, **kwargs):
        if Path(target) == path:
            calls.append(flags)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(identity_module.os, "open", recording_open)
    outcome = FileIdentityProbe(IdentityPolicy(allow_symlinks=allow_symlinks)).check(path)
    assert outcome.codes == ()
    assert len(calls) == 1
    flags = calls[0]
    assert flags & os.O_ACCMODE == os.O_RDONLY
    for flag_name in ["O_BINARY", "O_NONBLOCK"]:
        flag = getattr(os, flag_name, 0)
        assert flags & flag == flag
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    assert flags & no_follow == (0 if allow_symlinks else no_follow)


@pytest.mark.parametrize("replacement", ["fifo", "oversized"])
def test_opened_descriptor_is_rechecked_before_any_read(
    file_factory, tracked_stream_factory, monkeypatch, forbidden_operation, replacement,
):
    path = file_factory(content=b"x")
    stream = tracked_stream_factory(path)
    stream.read = forbidden_operation
    status = path.stat()
    changed = snapshot(status, **({"st_mode": stat.S_IFIFO} if replacement == "fifo" else {"st_size": 10}))
    descriptor_statuses(monkeypatch, stream, [changed])
    worker = FileIdentityProbe(IdentityPolicy(max_bytes=5))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == ((Code.NOT_A_REGULAR_FILE,) if replacement == "fifo" else (Code.TOO_LARGE,))
    if replacement == "oversized":
        assert outcome.facts["size_bytes"] == 10
        assert outcome.findings[0].detail["observed"] == 10
    assert "digest" not in outcome.facts
    assert stream.closed


@pytest.mark.parametrize("field", ["st_dev", "st_ino", "st_size", "st_mtime_ns"])
def test_metadata_changes_during_hashing_invalidate_digest(
    file_factory, tracked_stream_factory, monkeypatch, field,
):
    path = file_factory(content=b"abcde")
    before = path.stat()
    stream = tracked_stream_factory(path)
    after = snapshot(before, **{field: getattr(before, field) + 1})
    descriptor_statuses(monkeypatch, stream, [before, after])
    worker = FileIdentityProbe()
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == (Code.CHANGED_WHILE_READING,)
    assert dict(outcome.findings[0].detail) == {"expected_bytes": 5, "observed_bytes": 5}
    assert "digest" not in outcome.facts
    assert "digest_algorithm" not in outcome.facts
    assert stream.closed


@pytest.mark.parametrize("payload", [b"abcd", b"abcdef"])
def test_read_byte_count_must_match_declared_size_even_with_stable_metadata(
    file_factory, tracked_stream_factory, monkeypatch, payload,
):
    path = file_factory(content=b"abcde")
    stream = tracked_stream_factory(path, payload=payload)
    worker = FileIdentityProbe(IdentityPolicy(read_chunk_bytes=2))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert outcome.codes == (Code.CHANGED_WHILE_READING,)
    assert dict(outcome.findings[0].detail) == {"expected_bytes": 5, "observed_bytes": len(payload)}
    assert "digest" not in outcome.facts
    assert stream.closed


def test_real_file_growth_during_read_is_detected_without_threads_or_sleeps(
    file_factory, tracked_stream_factory, monkeypatch,
):
    path = file_factory(content=b"abcde")
    stream = tracked_stream_factory(path)
    original_read, appended = stream.read, False

    def growing_read(size):
        nonlocal appended
        chunk = original_read(size)
        if chunk and not appended:
            with path.open("ab") as writer:
                writer.write(b"more")
            appended = True
        return chunk

    stream.read = growing_read
    worker = FileIdentityProbe(IdentityPolicy(read_chunk_bytes=2))
    monkeypatch.setattr(worker, "_open", lambda path: stream)
    outcome = worker.check(path)
    assert appended
    assert outcome.codes == (Code.CHANGED_WHILE_READING,)
    assert outcome.findings[0].detail["expected_bytes"] == 5
    assert "digest" not in outcome.facts
    assert path.read_bytes() == b"abcdemore"
    assert stream.closed


def test_repeated_calls_have_independent_results(file_factory, missing_path):
    first_path, second_path = file_factory("first.txt", b"first"), file_factory("second.txt", b"second")
    worker = FileIdentityProbe()
    first = worker.check(first_path)
    missing = worker.check(missing_path)
    second = worker.check(second_path)
    assert first.codes == second.codes == ()
    assert missing.codes == (Code.NOT_FOUND,)
    assert dict(missing.facts) == {}
    assert first.facts["digest"] == hashlib.sha256(b"first").hexdigest()
    assert second.facts["digest"] == hashlib.sha256(b"second").hexdigest()
    assert first.facts["digest"] != second.facts["digest"]
    assert worker.check(first_path) == first
