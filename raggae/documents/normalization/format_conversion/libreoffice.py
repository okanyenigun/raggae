"""Render one Office document using a separately installed LibreOffice."""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, cast, Unpack, overload
from .office_models import (
    OfficeSourceFormat,
    OfficeToPdfFinding,
    OfficeToPdfIssue,
    OfficeToPdfOutcome,
    OfficeToPdfReport,
)
from ..io import SharedInputOutputHandler
from ..utils import require_executable, require_timeout_seconds
from ..models import NormalizationRequest, NormalizationResult
from ..request import NormalizationArguments, resolve_request


_FORMATS = frozenset({"docx", "xlsx", "pptx"})


class LibreOfficeToPdfConverter:
    """DOCX/XLSX/PPTX to PDF, with private scratch and a fresh native user profile."""

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
        if normalized not in _FORMATS:
            raise ValueError("source_format must be docx, xlsx, or pptx")
        executable_name = require_executable(executable)
        execution_timeout = require_timeout_seconds(timeout_seconds)
        self._source_format: OfficeSourceFormat = cast(OfficeSourceFormat, normalized)
        self._executable = executable_name
        self._timeout_seconds = execution_timeout
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "libreoffice_to_pdf_converter"

    @property
    def source_format(self) -> OfficeSourceFormat:
        return self._source_format

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[OfficeToPdfReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[OfficeToPdfReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[OfficeToPdfReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(report=report, artifact=artifact)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> OfficeToPdfReport:
        candidates = (
            (self._executable,)
            if self._executable is not None
            else ("soffice", "libreoffice")
        )
        executable = None
        for candidate in candidates:
            executable = shutil.which(candidate)
            if executable is not None:
                break
        if executable is None:
            return self._failure(
                OfficeToPdfFinding.DEPENDENCY_UNAVAILABLE,
                "LibreOffice executable is unavailable; conversion was not attempted.",
            )
        executable = str(Path(executable).absolute())
        with TemporaryDirectory(prefix="raggae-libreoffice-") as directory:
            scratch = Path(directory)
            output_directory = scratch / "output"
            profile_directory = scratch / "profile"
            output_directory.mkdir()
            profile_directory.mkdir()
            source_path = scratch / f"source.{self._source_format}"
            output_path = output_directory / "source.pdf"
            with source_path.open("wb") as staged_source:
                shutil.copyfileobj(source, staged_source)
            try:
                completed = subprocess.run(
                    (
                        executable,
                        f"-env:UserInstallation={profile_directory.resolve().as_uri()}",
                        "--headless",
                        "--nologo",
                        "--nodefault",
                        "--norestore",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        str(output_directory),
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
                    OfficeToPdfFinding.TIMEOUT,
                    "LibreOffice conversion exceeded its execution timeout.",
                )
            except OSError:
                return self._failure(
                    OfficeToPdfFinding.DEPENDENCY_UNAVAILABLE,
                    "LibreOffice executable could not be started.",
                )
            if completed.returncode != 0:
                return self._failure(
                    OfficeToPdfFinding.CONVERSION_FAILED,
                    "LibreOffice could not convert the document.",
                )
            if output_path.is_symlink() or not output_path.is_file():
                return self._failure(
                    OfficeToPdfFinding.INVALID_OUTPUT,
                    "LibreOffice did not produce a PDF file.",
                )
            # A header check is not semantic output verification, which remains
            # separate. Source/scratch/destination storage errors propagate.
            converted = output_path.read_bytes()
            if not converted.startswith(b"%PDF-"):
                return self._failure(
                    OfficeToPdfFinding.INVALID_OUTPUT,
                    "LibreOffice output did not have a PDF header.",
                )

        offset = 0
        while offset < len(converted):
            written = output.write(converted[offset:])
            if written is None or written <= 0:
                raise OSError("destination did not accept PDF bytes")
            offset += written
        return OfficeToPdfReport(
            outcome=OfficeToPdfOutcome.CHANGED,
            source_format=self._source_format,
            output_size_bytes=len(converted),
        )

    def _failure(self, code: OfficeToPdfFinding, message: str) -> OfficeToPdfReport:
        return OfficeToPdfReport(
            outcome=OfficeToPdfOutcome.FAILED,
            source_format=self._source_format,
            issues=(OfficeToPdfIssue(code=code, message=message),),
        )
