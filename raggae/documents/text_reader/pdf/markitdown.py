"""Convert PDF pages to Markdown with Microsoft MarkItDown, one page at a time."""

import warnings
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ..utils.sources import failure, prepare_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class MarkItDownPdfTextReader:
    """Return one Markdown block per PDF page, converted by MarkItDown.

    MarkItDown converts a whole PDF into one Markdown text without page
    boundaries, so each page is copied into its own single-page PDF (with
    pikepdf, which also applies an ``Artifact`` password) and converted
    separately; every block keeps its ``page_index``. Only MarkItDown's PDF
    converter is enabled: no plugins, no OCR, and no fallback that would treat
    an unreadable PDF as plain text. MarkItDown reads with pdfminer, including
    text outside a page's visible area; such pages get a finding. Layout
    regions are unsupported.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY, FindingCode.CROP_NOT_APPLIED})

    @property
    def name(self) -> str:
        return "text_reader_markitdown_pdf"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        prepared = prepare_source(
            source,
            reader=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message=(
                "MarkItDownPdfTextReader converts whole pages, not layout regions."
            ),
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        try:
            if prepared.path is not None and not S_ISREG(prepared.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = prepared.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(FindingCode.DECODE_FAILED, "Source is empty.")
        if b"%PDF-" not in data[:1024]:
            return TextReadResult(
                reader=self.name,
                outcome="not_applicable",
                findings=(
                    MarkItDownPdfTextReader._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        try:
            import pikepdf
            from markitdown import (
                FileConversionException,
                MarkItDown,
                StreamInfo,
                UnsupportedFormatException,
            )
            from markitdown.converters import PdfConverter
        except ImportError as error:
            raise ImportError(
                "MarkItDown PDF conversion requires: pip install 'raggae[text-markitdown]'"
            ) from error

        # Only the PDF converter: no plugins, and no plain-text fallback.
        converter = MarkItDown(enable_builtins=False, enable_plugins=False)
        converter.register_converter(PdfConverter())
        info = StreamInfo(extension=".pdf", mimetype="application/pdf")
        password = prepared.password
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                document = pikepdf.Pdf.open(BytesIO(data), password=password or "")
        except pikepdf.PasswordError:
            code = (
                FindingCode.INVALID_PASSWORD
                if password
                else FindingCode.PASSWORD_REQUIRED
            )
            return self._failure(
                code, "PDF could not be opened with the supplied password."
            )
        except (pikepdf.PdfError, ValueError, OSError):
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")

        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        with document:
            if not len(document.pages):
                return self._failure(
                    FindingCode.NO_PAGES, "PDF contains no usable pages."
                )
            for index, page in enumerate(document.pages):
                try:
                    single = MarkItDownPdfTextReader._single_page(pikepdf, page)
                    text = converter.convert_stream(BytesIO(single), stream_info=info)
                    content = (text.text_content or "").replace("\f", "").strip()
                    cropped = MarkItDownPdfTextReader._cropped(page)
                except (
                    FileConversionException,
                    UnsupportedFormatException,
                    pikepdf.PdfError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    OSError,
                ):
                    findings.append(
                        MarkItDownPdfTextReader._finding(
                            FindingCode.PAGE_UNREADABLE,
                            "This PDF page could not be converted.",
                            page_index=index,
                        )
                    )
                    continue
                completed += 1
                if cropped:
                    findings.append(
                        MarkItDownPdfTextReader._finding(
                            FindingCode.CROP_NOT_APPLIED,
                            "This page's visible area is smaller than the page; text "
                            "outside it may be included.",
                            page_index=index,
                        )
                    )
                if content:
                    blocks.append(
                        TextBlock(
                            text=content,
                            page_index=index,
                            content_format=TextContentFormat.MARKDOWN,
                        )
                    )
                else:
                    findings.append(
                        MarkItDownPdfTextReader._finding(
                            FindingCode.EMPTY,
                            "No stored text found on this page; it may still contain "
                            "visible text in pixels.",
                            page_index=index,
                        )
                    )
        incomplete = any(
            f.code not in MarkItDownPdfTextReader._INFORMATIONAL for f in findings
        )
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _single_page(pikepdf: Any, page: Any) -> bytes:
        """A decrypted, standalone, unrotated copy of one page.

        The copy drops /Rotate so pdfminer groups text as written; on a rotated
        page it would otherwise stack letters one per line. MarkItDown returns no
        positions, so nothing else depends on rotation. The source is unchanged.
        """
        with pikepdf.Pdf.new() as single, BytesIO() as output:
            single.pages.append(page)
            copy = single.pages[0]
            if pikepdf.Name.Rotate in copy.obj:
                del copy.obj[pikepdf.Name.Rotate]
            single.save(output)
            return output.getvalue()

    @staticmethod
    def _cropped(page: Any) -> bool:
        """True when the visible crop is strictly smaller than the media box."""
        media = [float(value) for value in page.mediabox]
        crop = [float(value) for value in page.cropbox]
        m = (
            min(media[0], media[2]),
            min(media[1], media[3]),
            max(media[0], media[2]),
            max(media[1], media[3]),
        )
        c = (
            min(crop[0], crop[2]),
            min(crop[1], crop[3]),
            max(crop[0], crop[2]),
            max(crop[1], crop[3]),
        )
        return (
            c != m and m[0] <= c[0] and m[1] <= c[1] and c[2] <= m[2] and c[3] <= m[3]
        )

    @staticmethod
    def _finding(
        code: FindingCode, message: str, *, page_index: int | None = None
    ) -> TextReadFinding:
        return TextReadFinding(code=code, message=message, page_index=page_index)
