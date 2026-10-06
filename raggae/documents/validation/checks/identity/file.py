import os
import stat
import errno
import hashlib
from pathlib import Path
from typing import BinaryIO
from .policy import IdentityPolicy, IdentityFinding
from ..utils import require_path
from ...schemas.result import CheckOutcome, Finding, FactValue


class FileIdentityProbe:
    """
    Enforces the policy and hashes the contents in a single pass. Establishes the
    size, the digest, and the stable identity later checks verify against.

    Stops as soon as it learns the file must not be read. Unlike filename
    validation there is nothing to gain by collecting further findings: a path
    that is a directory has no size worth reporting, and a file over the limit
    must not be hashed.
    """

    def __init__(self, policy: IdentityPolicy | None = None) -> None:
        self._policy = policy or IdentityPolicy()

    # --- public interface ------------------------------------------------------

    def check(self, path: Path) -> CheckOutcome:
        target = require_path(path)

        try:
            link_status = target.lstat()
        except OSError as error:
            return CheckOutcome(findings=(self._open_failure(target, error),))

        symlink_finding = self._check_symlink(target, link_status)
        if symlink_finding:
            return CheckOutcome(findings=tuple(symlink_finding))

        try:
            status = target.stat()  # resolves a symlink the policy allows
        except OSError as error:
            return CheckOutcome(findings=(self._open_failure(target, error),))

        kind_finding = self._check_regular_file(status)
        if kind_finding:
            return CheckOutcome(findings=tuple(kind_finding))

        try:
            stream = self._open(target)
        except OSError as error:
            return CheckOutcome(findings=(self._open_failure(target, error),))

        try:
            with stream:
                return self._inspect_open_file(stream)
        except OSError as error:
            return CheckOutcome(
                findings=(
                    self._finding(
                        IdentityFinding.UNREADABLE,
                        f"File could not be inspected: {error.strerror or error}.",
                        path=str(target),
                    ),
                ),
            )

    # --- internal helpers ------------------------------------------------------
    @property
    def name(self) -> str:
        return "validator_identity_file"

    @property
    def policy(self) -> IdentityPolicy:
        return self._policy

    def _open_failure(self, path: Path, error: OSError) -> Finding:
        """Translate a failed open into the finding that explains it."""
        if isinstance(error, FileNotFoundError):
            return self._finding(
                IdentityFinding.NOT_FOUND,
                "No file exists at the given path.",
                path=str(path),
            )
        if isinstance(error, IsADirectoryError):
            return self._finding(
                IdentityFinding.NOT_A_REGULAR_FILE,
                "Path is not a regular file.",
                kind="directory",
            )
        if isinstance(error, PermissionError):
            return self._finding(
                IdentityFinding.UNREADABLE,
                "File exists but cannot be read.",
                path=str(path),
            )
        # O_NOFOLLOW refused a symlink that appeared after the lstat.
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            return self._finding(
                IdentityFinding.SYMLINK,
                "Path is a symbolic link, which resolves outside the submitted path.",
                path=str(path),
            )
        return self._finding(
            IdentityFinding.UNREADABLE,
            f"File could not be opened: {error.strerror or error}.",
            path=str(path),
        )

    def _check_symlink(self, path: Path, status: os.stat_result) -> list[Finding]:
        """Report a symlink before following it anywhere."""
        if self._policy.allow_symlinks or not stat.S_ISLNK(status.st_mode):
            return []
        return [
            self._finding(
                IdentityFinding.SYMLINK,
                "Path is a symbolic link, which resolves outside the submitted path.",
                path=str(path),
            )
        ]

    def _check_regular_file(self, status: os.stat_result) -> list[Finding]:
        if stat.S_ISREG(status.st_mode):
            return []
        return [
            self._finding(
                IdentityFinding.NOT_A_REGULAR_FILE,
                "Path is not a regular file.",
                kind=self._describe_kind(status.st_mode),
            )
        ]

    def _open(self, path: Path) -> BinaryIO:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        if not self._policy.allow_symlinks:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.fdopen(os.open(path, flags), mode="rb", closefd=True)

    def _inspect_open_file(self, stream: BinaryIO) -> CheckOutcome:
        status = os.fstat(stream.fileno())

        kind_finding = self._check_regular_file(status)
        if kind_finding:
            return CheckOutcome(findings=tuple(kind_finding))

        size_findings = self._check_size(status.st_size)
        facts = self._identity_facts(status)

        if any(f.code == IdentityFinding.TOO_LARGE for f in size_findings):
            # Never read a file we have already refused for its size.
            return CheckOutcome(findings=tuple(size_findings), facts=facts)

        if not self._policy.compute_digest:
            return CheckOutcome(findings=tuple(size_findings), facts=facts)

        digest, read_bytes = self._digest(stream)
        changed = self._check_unchanged(stream, status, read_bytes)
        if changed:
            return CheckOutcome(findings=tuple(size_findings + changed), facts=facts)

        return CheckOutcome(
            findings=tuple(size_findings),
            facts={
                **facts,
                "digest": digest,
                "digest_algorithm": self._policy.hash_algorithm,
            },
        )

    def _check_size(self, size: int) -> list[Finding]:
        """Report a file outside the accepted size range."""
        policy = self._policy
        if size > policy.max_bytes:
            return [
                self._finding(
                    IdentityFinding.TOO_LARGE,
                    "File is larger than the limit.",
                    limit=policy.max_bytes,
                    observed=size,
                )
            ]
        if size < policy.min_bytes:
            return [
                self._finding(
                    IdentityFinding.TOO_SMALL,
                    "File is smaller than the minimum.",
                    limit=policy.min_bytes,
                    observed=size,
                )
            ]
        return []

    def _digest(self, stream: BinaryIO) -> tuple[str, int]:
        """Hash the contents by streaming, so the file is never held in memory."""
        digest = hashlib.new(self._policy.hash_algorithm)
        read_bytes = 0
        while chunk := stream.read(self._policy.read_chunk_bytes):
            read_bytes += len(chunk)
            digest.update(chunk)
        return digest.hexdigest(), read_bytes

    def _check_unchanged(
        self,
        stream: BinaryIO,
        before: os.stat_result,
        read_bytes: int,
    ) -> list[Finding]:
        after = os.fstat(stream.fileno())
        unchanged = (
            self._identity_of(before) == self._identity_of(after)
            and read_bytes == before.st_size
        )
        if unchanged:
            return []
        return [
            self._finding(
                IdentityFinding.CHANGED_WHILE_READING,
                "File changed while its digest was being computed.",
                expected_bytes=before.st_size,
                observed_bytes=read_bytes,
            )
        ]

    @staticmethod
    def _finding(code: IdentityFinding, message: str, **detail: FactValue) -> Finding:
        return Finding(code=code, message=message, detail=detail)

    @staticmethod
    def _describe_kind(mode: int) -> str:
        for predicate, label in (
            (stat.S_ISDIR, "directory"),
            (stat.S_ISFIFO, "fifo"),
            (stat.S_ISSOCK, "socket"),
            (stat.S_ISCHR, "character device"),
            (stat.S_ISBLK, "block device"),
            (stat.S_ISLNK, "symlink"),
        ):
            if predicate(mode):
                return label
        return "unknown"

    @staticmethod
    def _identity_facts(status: os.stat_result) -> dict[str, FactValue]:
        return {
            "size_bytes": status.st_size,
            "device": status.st_dev,
            "inode": status.st_ino,
            "mtime_ns": status.st_mtime_ns,
        }

    @staticmethod
    def _identity_of(status: os.stat_result) -> tuple[int, int, int, int]:
        return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
