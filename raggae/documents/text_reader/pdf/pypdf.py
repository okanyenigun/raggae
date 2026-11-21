"""Read stored PDF text page by page with pypdf; whole pages only."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal
from ..utils.sources import failure, prepare_source
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


type PypdfMode = Literal["plain", "layout"]


class PypdfTextReader:
    """Return one plain-text block per page that has stored text, using pypdf.

    ``mode="plain"`` (default) gives text in content order; ``mode="layout"``
    keeps columns side by side with spaces. No OCR: pages whose text exists
    only as pixels are reported empty. pypdf also reads text outside a page's
    visible (cropped) area; pages with a crop smaller than the page get a
    finding. Layout-region requests are unsupported; use
    ``PdfplumberTextReader`` for those. Passwords come from ``Artifact``.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY, FindingCode.CROP_NOT_APPLIED})

    def __init__(self, *, mode: PypdfMode = "plain") -> None:
        if not isinstance(mode, str):
            raise TypeError("mode must be a string")
        if mode not in ("plain", "layout"):
            raise ValueError("mode must be 'plain' or 'layout'")
        self._mode: PypdfMode = mode

    @property
    def name(self) -> str:
        return "text_reader_pypdf"

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
                "PypdfTextReader reads whole pages, not layout regions; "
                "use PdfplumberTextReader for region reads."
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
                    PypdfTextReader._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )

        from pypdf import PdfReader
        from pypdf.errors import FileNotDecryptedError, PyPdfError

        errors: tuple[type[BaseException], ...] = (
            PyPdfError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            AssertionError,
            ZeroDivisionError,
            OverflowError,
            RecursionError,
            OSError,
        )
        password = prepared.password
        try:
            document = PdfReader(BytesIO(data))
            if document.is_encrypted and password:
                if not document.decrypt(password):
                    return self._failure(
                        FindingCode.INVALID_PASSWORD,
                        "PDF could not be opened with the supplied password.",
                    )
            pages = list(document.pages)
        except FileNotDecryptedError:
            return self._password_required()
        except errors:
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        if not pages:
            return self._failure(FindingCode.NO_PAGES, "PDF contains no usable pages.")

        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        for index, page in enumerate(pages):
            try:
                text = page.extract_text(extraction_mode=self._mode)
                cropped = PypdfTextReader._cropped(page)
            except FileNotDecryptedError:
                return self._password_required()
            except errors:
                findings.append(
                    PypdfTextReader._finding(
                        FindingCode.PAGE_UNREADABLE,
                        "Stored text on this PDF page could not be read.",
                        page_index=index,
                    )
                )
                continue
            completed += 1
            if cropped:
                findings.append(
                    PypdfTextReader._finding(
                        FindingCode.CROP_NOT_APPLIED,
                        "This page's visible area is smaller than the page; text "
                        "outside it may be included.",
                        page_index=index,
                    )
                )
            if text.strip():
                blocks.append(TextBlock(text=text, page_index=index))
            else:
                findings.append(
                    PypdfTextReader._finding(
                        FindingCode.EMPTY,
                        "No stored text found on this page; it may still contain "
                        "visible text in pixels.",
                        page_index=index,
                    )
                )
        incomplete = any(f.code not in PypdfTextReader._INFORMATIONAL for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _password_required(self) -> TextReadResult:
        return self._failure(
            FindingCode.PASSWORD_REQUIRED,
            "PDF could not be opened with the supplied password.",
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _cropped(page: Any) -> bool:
        """True when the visible crop is strictly smaller than the media box."""
        crop = [float(value) for value in page.cropbox]
        media = [float(value) for value in page.mediabox]
        left, bottom, right, top = (
            min(crop[0], crop[2]),
            min(crop[1], crop[3]),
            max(crop[0], crop[2]),
            max(crop[1], crop[3]),
        )
        m_left, m_bottom, m_right, m_top = (
            min(media[0], media[2]),
            min(media[1], media[3]),
            max(media[0], media[2]),
            max(media[1], media[3]),
        )
        return (left, bottom, right, top) != (m_left, m_bottom, m_right, m_top) and (
            left >= m_left and bottom >= m_bottom and right <= m_right and top <= m_top
        )

    @staticmethod
    def _finding(
        code: FindingCode, message: str, *, page_index: int | None = None
    ) -> TextReadFinding:
        return TextReadFinding(code=code, message=message, page_index=page_index)
