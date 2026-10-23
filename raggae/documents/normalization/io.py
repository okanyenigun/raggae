"""Opening, closing, and successful-output handling for one worker call."""

import os
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile
from typing import BinaryIO, cast
from .artifacts import Artifact


class SharedInputOutputHandler:
    """Handle resources; the supplied transformation handles document content."""

    def execute[ReportT](
        self,
        *,
        source: Artifact,
        output_path: Path | None,
        transform: Callable[[BinaryIO, BinaryIO], ReportT],
    ) -> tuple[ReportT, Artifact | None]:
        if not isinstance(source, Artifact):
            raise TypeError("source must be an Artifact")
        if output_path is not None and not isinstance(output_path, Path):
            raise TypeError("output_path must be a Path or None")
        if not callable(transform):
            raise TypeError("transform must be callable")

        with self._open_source(source) as reader:
            if output_path is None:
                with BytesIO() as output:
                    report = transform(reader, output)
                    candidate = Artifact(data=output.getvalue())
                return report, self._select_artifact(report, source, candidate)

            if os.path.lexists(output_path):
                raise FileExistsError(f"destination already exists: {output_path}")
            staging = None
            try:
                # Private scratch output is only for this call, not a returned
                # temporary-storage mode. Close it before publishing the file.
                with NamedTemporaryFile(
                    mode="w+b",
                    prefix=".raggae-",
                    suffix=".part",
                    dir=output_path.parent,
                    delete=False,
                ) as temporary_output:
                    staging = Path(temporary_output.name)
                    # The wrapper delegates binary I/O to its underlying file,
                    # but typeshed does not declare it as a BinaryIO subclass.
                    report = transform(reader, cast(BinaryIO, temporary_output))
                candidate = Artifact(path=output_path)
                artifact = self._select_artifact(report, source, candidate)
                if artifact is candidate:
                    # A hard link publishes complete bytes without replacing a
                    # destination created by somebody else during the call.
                    os.link(staging, output_path)
                return report, artifact
            finally:
                if staging is not None:
                    staging.unlink(missing_ok=True)

    @staticmethod
    def _select_artifact(
        report: object, original: Artifact, candidate: Artifact
    ) -> Artifact | None:
        outcome = getattr(report, "outcome", None)
        if outcome == "changed":
            return candidate
        if outcome == "no_change":
            return original
        if outcome == "failed":
            return None
        raise ValueError("transformation returned an unknown outcome")

    @staticmethod
    def _open_source(source: Artifact) -> BinaryIO:
        if source.path is None:
            assert source.data is not None
            return BytesIO(source.data)
        # A seekable on-disk document must be a regular file. Non-blocking open
        # prevents a mistaken FIFO path from hanging before that check.
        descriptor = os.open(source.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("source path must identify a regular file")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
