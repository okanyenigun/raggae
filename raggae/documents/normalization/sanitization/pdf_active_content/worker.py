"""Remove a documented, limited set of PDF actions and interactive content."""

import pikepdf
from collections import Counter
from typing import BinaryIO, Unpack, overload
from .models import (
    PdfActiveContentCategory,
    PdfActiveContentFinding,
    PdfActiveContentIssue,
    PdfActiveContentOutcome,
    PdfActiveContentReport,
)
from ...io import SharedInputOutputHandler
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


_ACTION_CATEGORIES = {
    "/JavaScript": PdfActiveContentCategory.SCRIPT,
    "/Launch": PdfActiveContentCategory.LAUNCH_ACTION,
    "/URI": PdfActiveContentCategory.REMOTE_REFERENCE,
    "/GoToR": PdfActiveContentCategory.REMOTE_REFERENCE,
}


class PdfActiveContentSanitizer:
    """Explicitly remove selected features; do not claim a PDF is harmless."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_pdf_active_content"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[PdfActiveContentReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[PdfActiveContentReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[PdfActiveContentReport]:
        request = resolve_request(request, kwargs)

        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> PdfActiveContentReport:
        try:
            document = pikepdf.Pdf.open(
                source,
                password="",
                attempt_recovery=False,
                allow_overwriting_input=False,
            )
        except pikepdf.PasswordError:
            return self._failure(
                PdfActiveContentFinding.PASSWORD_REQUIRED,
                "PDF requires a password; use PDF access normalization first.",
            )
        except (pikepdf.PdfError, ValueError):
            return self._failure(
                PdfActiveContentFinding.DECODE_FAILED,
                "PDF could not be opened strictly.",
            )
        with document:
            try:
                removals = self._remove_active_content(document)
                page_count = len(document.pages)
            except (pikepdf.PdfError, TypeError, ValueError):
                return self._failure(
                    PdfActiveContentFinding.SANITIZATION_FAILED,
                    "PDF active content could not be removed safely.",
                )
            if not removals:
                return PdfActiveContentReport(
                    outcome=PdfActiveContentOutcome.NO_CHANGE,
                    page_count=page_count,
                )
            try:
                document.save(output, encryption=False)
            except (pikepdf.PdfError, TypeError, ValueError):
                return self._failure(
                    PdfActiveContentFinding.ENCODE_FAILED,
                    "Sanitized PDF could not be encoded.",
                )
        return PdfActiveContentReport(
            outcome=PdfActiveContentOutcome.CHANGED,
            page_count=page_count,
            removals=dict(removals),
        )

    @staticmethod
    def _remove_active_content(
        document: pikepdf.Pdf,
    ) -> Counter[PdfActiveContentCategory]:
        removed: Counter[PdfActiveContentCategory] = Counter()
        root = document.Root
        names = root.get("/Names")
        if names is not None:
            PdfActiveContentSanitizer._require_dictionary(names)
            removed_tree = False
            for key, category in (
                ("/JavaScript", PdfActiveContentCategory.SCRIPT),
                ("/EmbeddedFiles", PdfActiveContentCategory.EMBEDDED_FILE),
            ):
                if key in names:
                    del names[key]
                    removed[category] += 1
                    removed_tree = True
            if removed_tree and not names:
                del root["/Names"]

        for key in ("/OpenAction", "/AA"):
            if key in root:
                del root[key]
                removed[PdfActiveContentCategory.AUTOMATIC_ACTION] += 1

        for page in document.pages:
            if "/AA" in page.obj:
                del page.obj["/AA"]
                removed[PdfActiveContentCategory.AUTOMATIC_ACTION] += 1
            annotations = page.obj.get("/Annots")
            if annotations is None:
                continue
            if not isinstance(annotations, pikepdf.Array):
                raise ValueError("annotation collection must be an array")
            index = 0
            while index < len(annotations):
                annotation = annotations[index]
                PdfActiveContentSanitizer._require_dictionary(annotation)
                subtype = str(annotation.get("/Subtype", ""))
                category = {
                    "/FileAttachment": PdfActiveContentCategory.EMBEDDED_FILE,
                    "/RichMedia": PdfActiveContentCategory.RICH_MEDIA,
                }.get(subtype)
                if category is not None:
                    del annotations[index]
                    removed[category] += 1
                    continue
                if "/AA" in annotation:
                    del annotation["/AA"]
                    removed[PdfActiveContentCategory.AUTOMATIC_ACTION] += 1
                action = annotation.get("/A")
                if action is not None:
                    PdfActiveContentSanitizer._require_dictionary(action)
                    category = _ACTION_CATEGORIES.get(str(action.get("/S", "")))
                    if category is not None:
                        del annotation["/A"]
                        removed[category] += 1
                index += 1
        return removed

    @staticmethod
    def _require_dictionary(value: object) -> None:
        if not isinstance(value, pikepdf.Dictionary):
            raise ValueError("expected a PDF dictionary")

    @staticmethod
    def _failure(code: PdfActiveContentFinding, message: str) -> PdfActiveContentReport:
        return PdfActiveContentReport(
            outcome=PdfActiveContentOutcome.FAILED,
            issues=(PdfActiveContentIssue(code=code, message=message),),
        )
