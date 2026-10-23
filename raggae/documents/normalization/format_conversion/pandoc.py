"""One declared conversion route, executed by a separately installed Pandoc."""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, cast, Unpack, overload
from .html_models import (
    DocumentToHtmlFinding,
    DocumentToHtmlIssue,
    DocumentToHtmlOutcome,
    DocumentToHtmlReport,
    HtmlSourceFormat,
)
from ..io import SharedInputOutputHandler
from ..utils import require_executable, require_timeout_seconds
from ..models import NormalizationRequest, NormalizationResult
from ..request import NormalizationArguments, resolve_request


_FORMATS = frozenset({"docx", "odt", "rtf", "epub", "markdown"})


class DocumentToHtmlConverter:
    """Create standalone HTML5; shared I/O handles the real source/destination."""

    def __init__(
        self,
        source_format: str = "docx",
        *,
        executable: str | Path | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        if not isinstance(source_format, str):
            raise TypeError("source_format must be a string")
        normalized = source_format.strip().removeprefix(".").casefold()
        if normalized == "md":
            normalized = "markdown"
        if normalized not in _FORMATS:
            raise ValueError(
                "source_format must be docx, odt, rtf, epub, markdown, or md"
            )
        executable_name = require_executable(executable)
        execution_timeout = require_timeout_seconds(timeout_seconds)
        self._source_format: HtmlSourceFormat = cast(HtmlSourceFormat, normalized)
        self._executable = executable_name
        self._timeout_seconds = execution_timeout
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_document_to_html"

    @property
    def source_format(self) -> HtmlSourceFormat:
        return self._source_format

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[DocumentToHtmlReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[DocumentToHtmlReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[DocumentToHtmlReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(report=report, artifact=artifact)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> DocumentToHtmlReport:
        executable = shutil.which(self._executable or "pandoc")
        if executable is None:
            return self._failure(
                DocumentToHtmlFinding.DEPENDENCY_UNAVAILABLE,
                "Pandoc executable is unavailable; conversion was not attempted.",
            )
        # Relative executable paths must remain usable after changing to scratch.
        executable = str(Path(executable).absolute())
        with TemporaryDirectory(prefix="raggae-pandoc-") as directory:
            scratch = Path(directory)
            source_path = scratch / f"source.{self._source_format}"
            output_path = scratch / "converted.html"
            with source_path.open("wb") as staged_source:
                shutil.copyfileobj(source, staged_source)
            try:
                completed = subprocess.run(
                    (
                        executable,
                        "--sandbox",
                        f"--from={self._source_format}",
                        "--to=html5",
                        "--standalone",
                        f"--output={output_path}",
                        str(source_path),
                    ),
                    shell=False,
                    check=False,
                    cwd=scratch,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return self._failure(
                    DocumentToHtmlFinding.TIMEOUT,
                    "Pandoc conversion exceeded its execution timeout.",
                )
            except OSError:
                return self._failure(
                    DocumentToHtmlFinding.DEPENDENCY_UNAVAILABLE,
                    "Pandoc executable could not be started.",
                )
            if completed.returncode != 0:
                return self._failure(
                    DocumentToHtmlFinding.CONVERSION_FAILED,
                    "Pandoc could not convert the document.",
                )
            if output_path.is_symlink() or not output_path.is_file():
                return self._failure(
                    DocumentToHtmlFinding.INVALID_OUTPUT,
                    "Pandoc did not produce an HTML file.",
                )
            # Source, scratch, and destination I/O errors propagate after cleanup.
            converted = output_path.read_bytes()
            try:
                text = converted.decode("utf-8")
            except UnicodeDecodeError:
                return self._failure(
                    DocumentToHtmlFinding.INVALID_OUTPUT,
                    "Pandoc output was not UTF-8 HTML.",
                )
            if not text.strip():
                return self._failure(
                    DocumentToHtmlFinding.INVALID_OUTPUT, "Pandoc output was empty."
                )

        offset = 0
        while offset < len(converted):
            written = output.write(converted[offset:])
            if written is None or written <= 0:
                raise OSError("destination did not accept HTML bytes")
            offset += written
        return DocumentToHtmlReport(
            outcome=DocumentToHtmlOutcome.CHANGED,
            source_format=self._source_format,
            output_size_bytes=len(converted),
        )

    def _failure(
        self, code: DocumentToHtmlFinding, message: str
    ) -> DocumentToHtmlReport:
        return DocumentToHtmlReport(
            outcome=DocumentToHtmlOutcome.FAILED,
            source_format=self._source_format,
            issues=(DocumentToHtmlIssue(code=code, message=message),),
        )
