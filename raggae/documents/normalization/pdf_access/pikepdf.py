"""Create an unencrypted working copy only when a PDF needs a password."""

import pikepdf
from functools import partial
from typing import BinaryIO, Unpack, overload
from .models import PdfAccessFinding, PdfAccessIssue, PdfAccessOutcome, PdfAccessReport
from ..io import SharedInputOutputHandler
from ..models import NormalizationRequest, NormalizationResult
from ..request import NormalizationArguments, resolve_request


class PdfDecryptionNormalizer:
    """Open strictly, without repairing, sanitizing, or modifying the original."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_pdf_decryption"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[PdfAccessReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[PdfAccessReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[PdfAccessReport]:
        request = resolve_request(request, kwargs)

        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=partial(self._transform, password=request.password),
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(
        self, source: BinaryIO, output: BinaryIO, *, password: str | bytes | None
    ) -> PdfAccessReport:
        try:
            document = self._open(source, password="")
        except pikepdf.PasswordError:
            return self._unlock(source, output, password)
        except (pikepdf.PdfError, ValueError):
            return self._failure(
                PdfAccessFinding.DECODE_FAILED, "PDF could not be opened strictly."
            )
        with document:
            try:
                return PdfAccessReport(
                    outcome=PdfAccessOutcome.NO_CHANGE,
                    source_encrypted=bool(document.is_encrypted),
                    password_required=False,
                    page_count=len(document.pages),
                )
            except (pikepdf.PdfError, ValueError):
                return self._failure(
                    PdfAccessFinding.DECODE_FAILED, "PDF could not be read strictly."
                )

    def _unlock(
        self, source: BinaryIO, output: BinaryIO, password: str | bytes | None
    ) -> PdfAccessReport:
        if password is None or password in ("", b""):
            return self._failure(
                PdfAccessFinding.PASSWORD_REQUIRED,
                "PDF requires a non-empty password.",
                source_encrypted=True,
                password_required=True,
            )
        source.seek(0)
        try:
            document = self._open(source, password=password)
        except pikepdf.PasswordError:
            return self._failure(
                PdfAccessFinding.DECRYPT_FAILED,
                "PDF could not be opened with the supplied password.",
                source_encrypted=True,
                password_required=True,
            )
        except (pikepdf.PdfError, ValueError):
            return self._failure(
                PdfAccessFinding.DECODE_FAILED,
                "PDF could not be opened strictly.",
                source_encrypted=True,
                password_required=True,
            )
        with document:
            try:
                page_count = len(document.pages)
            except (pikepdf.PdfError, ValueError):
                return self._failure(
                    PdfAccessFinding.DECODE_FAILED,
                    "PDF could not be read strictly.",
                    source_encrypted=True,
                    password_required=True,
                )
            try:
                document.save(output, encryption=False)
            except (pikepdf.PdfError, TypeError, ValueError):
                return self._failure(
                    PdfAccessFinding.ENCODE_FAILED,
                    "Unlocked PDF could not be encoded.",
                    source_encrypted=True,
                    password_required=True,
                    page_count=page_count,
                )
        return PdfAccessReport(
            outcome=PdfAccessOutcome.CHANGED,
            source_encrypted=True,
            password_required=True,
            page_count=page_count,
        )

    @staticmethod
    def _open(source: BinaryIO, *, password: str | bytes) -> pikepdf.Pdf:
        return pikepdf.Pdf.open(
            source,
            password=password,
            attempt_recovery=False,
            allow_overwriting_input=False,
        )

    @staticmethod
    def _failure(
        code: PdfAccessFinding,
        message: str,
        *,
        source_encrypted: bool | None = None,
        password_required: bool | None = None,
        page_count: int | None = None,
    ) -> PdfAccessReport:
        return PdfAccessReport(
            outcome=PdfAccessOutcome.FAILED,
            source_encrypted=source_encrypted,
            password_required=password_required,
            page_count=page_count,
            issues=(PdfAccessIssue(code=code, message=message),),
        )
