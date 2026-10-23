"""Select or accept a worker sequence and publish only the successful final copy."""

from dataclasses import replace
import errno
import os
from pathlib import Path
import re
from tempfile import mkdtemp
from typing import BinaryIO, Unpack, overload
from pydantic import BaseModel
from .artifacts import Artifact
from .base import NormalizationWorker
from .format_conversion import (
    DocumentToHtmlConverter,
    LibreOfficeToPdfConverter,
    PillowImageToPdfConverter,
)
from .image_orientation import PillowImageOrientationNormalizer
from .io import SharedInputOutputHandler
from .models import NormalizationRequest, NormalizationResult
from .request import NormalizationArguments, resolve_request
from .pdf_access import PdfDecryptionNormalizer
from .report import DocumentNormalizationReport, NormalizationStepReport
from .sanitization import (
    CsvFormulaSanitizer,
    HtmlActiveContentSanitizer,
    HtmlMetadataSanitizer,
    ImageMetadataSanitizer,
    OoxmlActiveContentSanitizer,
    OoxmlMetadataSanitizer,
    PdfActiveContentSanitizer,
    PdfMetadataSanitizer,
)
from .text_encoding import StrictTextEncodingNormalizer
from .utils import require_path, write_all_bytes


class DocumentNormalizer:
    """Pass each worker's artifact to the next; stop on the first failed result."""

    def __init__(
        self,
        *,
        workers: tuple[NormalizationWorker, ...] | None = None,
        intermediate_dir: Path | str | None = None,
        step_output_paths: tuple[Path | str | None, ...]
        | list[Path | str | None]
        | None = None,
    ) -> None:
        self._workers = None if workers is None else tuple(workers)
        self._intermediate_dir = (
            None if intermediate_dir is None else require_path(intermediate_dir)
        )
        if step_output_paths is not None and not isinstance(step_output_paths, (tuple, list)):
            raise TypeError("step_output_paths must be a tuple, list, or None")
        self._step_output_paths = (
            None if step_output_paths is None else tuple(
                None if path is None else require_path(path)
                for path in step_output_paths
            )
        )
        self._io_handler = SharedInputOutputHandler()

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[DocumentNormalizationReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[DocumentNormalizationReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[DocumentNormalizationReport]:
        request = resolve_request(request, kwargs)

        workers = (
            self._default_workers(request) if self._workers is None else self._workers
        )
        self._validate_step_paths(request, len(workers))
        current = request.source
        changed = False
        steps: list[NormalizationStepReport] = []
        run_directory: Path | None = None
        source_format = request.source_format
        if source_format is None and current.path is not None:
            source_format = current.path.suffix.removeprefix(".").casefold()
        try:
            for index, worker in enumerate(workers, 1):
                automatic_path = False
                if self._step_output_paths is not None:
                    destination = self._step_output_paths[index - 1]
                elif self._intermediate_dir is not None:
                    if run_directory is None:
                        run_directory = Path(mkdtemp(prefix="run-", dir=self._intermediate_dir))
                    # The report identifies the actual output format after the
                    # worker runs. Until then, use an extension-free filename.
                    destination = run_directory / self._automatic_stem(index, worker.name)
                    automatic_path = True
                else:
                    destination = None

                # The final destination remains private to the coordinator.
                # Workers receive only their own intermediate destination.
                result = worker.normalize(
                    replace(request, source=current, output_path=destination)
                )
                if result.outcome != "failed":
                    assert result.artifact is not None
                    current = result.artifact
                    source_format = self._output_format(worker, result.report, source_format)
                    if automatic_path and result.outcome == "changed" and current.path == destination:
                        assert destination is not None
                        named_path = destination.with_suffix("." + source_format)
                        # Complete bytes only, without overwriting a file even
                        # if another process creates the named destination.
                        os.link(destination, named_path)
                        destination.unlink()
                        current = Artifact(path=named_path)
                steps.append(
                    NormalizationStepReport(
                        worker=worker.name,
                        report=result.report,
                        output_path=current.path if result.outcome == "changed" else None,
                    )
                )
                if result.outcome == "failed":
                    return NormalizationResult(
                        artifact=None,
                        report=DocumentNormalizationReport(
                            outcome="failed", steps=tuple(steps)
                        ),
                    )
                changed = changed or result.outcome == "changed"

            report = DocumentNormalizationReport(
                outcome="changed" if changed else "no_change", steps=tuple(steps)
            )
            if not changed:
                return NormalizationResult(artifact=request.source, report=report)
            if request.output_path is None:
                return NormalizationResult(artifact=current, report=report)

            def publish(source: BinaryIO, output: BinaryIO) -> DocumentNormalizationReport:
                while chunk := source.read(64 * 1024):
                    write_all_bytes(output, chunk)
                return report

            published_report, artifact = self._io_handler.execute(
                source=current, output_path=request.output_path, transform=publish
            )
            return NormalizationResult(artifact=artifact, report=published_report)
        finally:
            if run_directory is not None:
                # Only remove an empty directory created by this call. Completed
                # step files survive both successful and failed chains.
                try:
                    run_directory.rmdir()
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                        raise

    def _validate_step_paths(self, request: NormalizationRequest, worker_count: int) -> None:
        if self._step_output_paths is None:
            return
        if len(self._step_output_paths) != worker_count:
            raise ValueError("step_output_paths must have one entry for each selected worker")
        forbidden = {
            path.resolve() for path in (request.source.path, request.output_path)
            if path is not None
        }
        seen: set[Path] = set()
        for path in self._step_output_paths:
            if path is None:
                continue
            canonical = path.resolve()
            if canonical in seen or canonical in forbidden:
                raise ValueError("step_output_paths must be distinct from each other, the source, and the final destination")
            seen.add(canonical)
            if os.path.lexists(path):
                raise FileExistsError(f"destination already exists: {path}")
            if not path.parent.exists():
                raise FileNotFoundError(f"destination parent does not exist: {path.parent}")
            if not path.parent.is_dir():
                raise NotADirectoryError(f"destination parent is not a directory: {path.parent}")

    @staticmethod
    def _automatic_stem(index: int, worker_name: str) -> str:
        name = worker_name.removeprefix("normalization_")
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_-")[:80] or "worker"
        return f"{index:02d}-{name}"

    @staticmethod
    def _output_format(
        worker: NormalizationWorker, report: BaseModel, previous_format: str | None
    ) -> str:
        # Naming is coordination, not a new requirement on worker protocols.
        # Reported image/package formats take precedence over input extensions.
        if isinstance(worker, (
            PdfDecryptionNormalizer, PdfActiveContentSanitizer,
            PdfMetadataSanitizer, PillowImageToPdfConverter, LibreOfficeToPdfConverter,
        )):
            return "pdf"
        if isinstance(worker, (HtmlActiveContentSanitizer, HtmlMetadataSanitizer, DocumentToHtmlConverter)):
            return "html"
        if isinstance(worker, CsvFormulaSanitizer):
            return "csv"
        if isinstance(worker, (PillowImageOrientationNormalizer, ImageMetadataSanitizer)):
            value = getattr(report, "image_format", None)
        elif isinstance(worker, (OoxmlActiveContentSanitizer, OoxmlMetadataSanitizer)):
            value = getattr(report, "document_format", None)
        elif isinstance(worker, StrictTextEncodingNormalizer):
            value = previous_format
        else:
            return "bin"
        if not isinstance(value, str):
            return "bin"
        value = {"jpeg": "jpg", "tif": "tiff", "markdown": "md"}.get(value.casefold(), value.casefold())
        return value if re.fullmatch(r"[a-z0-9]{1,16}", value) else "bin"

    @staticmethod
    def _default_workers(
        request: NormalizationRequest,
    ) -> tuple[NormalizationWorker, ...]:
        source_format = request.source_format
        if source_format is None and request.source.path is not None:
            source_format = request.source.path.suffix.removeprefix(".").casefold()
        if not source_format:
            raise ValueError(
                "source_format is required for in-memory sources or paths without an extension"
            )
        if source_format in {"html", "csv"} and request.target_encoding != "utf-8":
            raise ValueError("automatic HTML/CSV flows require target_encoding='utf-8'")

        if source_format == "pdf":
            return (
                PdfDecryptionNormalizer(),
                PdfActiveContentSanitizer(),
                PdfMetadataSanitizer(),
            )
        if source_format in {"docx", "xlsx", "pptx"}:
            return (OoxmlActiveContentSanitizer(), OoxmlMetadataSanitizer())
        if source_format in {"jpg", "jpeg", "png", "tif", "tiff"}:
            return (PillowImageOrientationNormalizer(), ImageMetadataSanitizer())
        if source_format == "html":
            return (
                StrictTextEncodingNormalizer(),
                HtmlActiveContentSanitizer(),
                HtmlMetadataSanitizer(),
            )
        if source_format == "csv":
            return (StrictTextEncodingNormalizer(), CsvFormulaSanitizer())
        if source_format in {"txt", "md", "json", "jsonl"}:
            return (StrictTextEncodingNormalizer(),)
        raise ValueError(
            f"no default normalization flow for source_format: {source_format}"
        )
