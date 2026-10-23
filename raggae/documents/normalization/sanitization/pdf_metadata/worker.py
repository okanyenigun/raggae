"""Remove trailer document information and catalogue XMP from accessible PDFs."""

import pikepdf
from typing import BinaryIO, Unpack, overload
from .models import (
    PdfMetadataFinding,
    PdfMetadataIssue,
    PdfMetadataOutcome,
    PdfMetadataReport,
)
from ...io import SharedInputOutputHandler
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


class PdfMetadataSanitizer:
    """Opt-in standard metadata removal; not anonymity or active-content cleanup."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_pdf_metadata"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[PdfMetadataReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[PdfMetadataReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[PdfMetadataReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> PdfMetadataReport:
        try:
            document = pikepdf.Pdf.open(
                source,
                password="",
                attempt_recovery=False,
                allow_overwriting_input=False,
            )
        except pikepdf.PasswordError:
            return self._failure(
                PdfMetadataFinding.PASSWORD_REQUIRED,
                "PDF requires a password; use PDF access normalization first.",
            )
        except (pikepdf.PdfError, ValueError):
            return self._failure(
                PdfMetadataFinding.DECODE_FAILED, "PDF could not be opened strictly."
            )
        with document:
            try:
                # Accessing document.docinfo can create /Info when none exists.
                # Inspect the actual entries instead, including empty dictionaries.
                info_removed = "/Info" in document.trailer
                xmp_removed = "/Metadata" in document.Root
                if info_removed:
                    del document.trailer["/Info"]
                if xmp_removed:
                    del document.Root["/Metadata"]
                page_count = len(document.pages)
            except (pikepdf.PdfError, TypeError, ValueError):
                return self._failure(
                    PdfMetadataFinding.SANITIZATION_FAILED,
                    "PDF metadata could not be removed safely.",
                )
            if not info_removed and not xmp_removed:
                return PdfMetadataReport(
                    outcome=PdfMetadataOutcome.NO_CHANGE,
                    page_count=page_count,
                )
            try:
                document.save(output, encryption=False, fix_metadata_version=False)
            except (pikepdf.PdfError, TypeError, ValueError):
                return self._failure(
                    PdfMetadataFinding.ENCODE_FAILED,
                    "Sanitized PDF could not be encoded.",
                )
        return PdfMetadataReport(
            outcome=PdfMetadataOutcome.CHANGED,
            document_info_removed=info_removed,
            xmp_removed=xmp_removed,
            page_count=page_count,
        )

    @staticmethod
    def _failure(code: PdfMetadataFinding, message: str) -> PdfMetadataReport:
        return PdfMetadataReport(
            outcome=PdfMetadataOutcome.FAILED,
            issues=(PdfMetadataIssue(code=code, message=message),),
        )
