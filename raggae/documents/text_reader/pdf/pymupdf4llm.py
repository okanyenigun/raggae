"""Convert PDF pages to structured Markdown with PyMuPDF4LLM, without OCR.

PyMuPDF4LLM and its ``pymupdf_layout`` model are licensed AGPL-3.0 or under an
Artifex commercial licence; they are installed only through the opt-in
``raggae[text-pymupdf4llm]`` extra.
"""

from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
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


class PyMuPdf4LlmTextReader:
    """Return one structured Markdown block per PDF page, from PyMuPDF4LLM.

    PyMuPDF4LLM's layout model detects headings, paragraphs, lists, and tables.
    OCR is always off (its default is on); pages whose text exists only as
    pixels are reported empty. Page headers and footers are kept, as the
    backend does by default. Rotation is reset on an in-memory copy, because
    PyMuPDF4LLM 1.28.2 returns no text for rotated pages; the source is never
    modified. Layout regions are unsupported. Importing PyMuPDF4LLM activates
    PyMuPDF's layout engine for the whole process; this does not change
    ``PyMuPdfTextReader``'s output. Requires ``raggae[text-pymupdf4llm]``.
    """

    _INFORMATIONAL = frozenset({FindingCode.EMPTY})
    # The native profile: never OCR, never write or embed images, no progress output.
    _NATIVE = {
        "page_chunks": True,
        "use_ocr": False,
        "force_ocr": False,
        "write_images": False,
        "embed_images": False,
        "show_progress": False,
    }

    @property
    def name(self) -> str:
        return "text_reader_pymupdf4llm"

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
                "PyMuPdf4LlmTextReader converts whole pages, not layout regions."
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
                    PyMuPdf4LlmTextReader._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        try:
            import pymupdf
            import pymupdf4llm
        except ImportError as error:
            raise ImportError(
                "PyMuPDF4LLM conversion requires: pip install 'raggae[text-pymupdf4llm]' "
                "(PyMuPDF4LLM is AGPL-3.0 or Artifex commercial)"
            ) from error
        password = prepared.password
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError, ValueError):
            return self._failure(FindingCode.DECODE_FAILED, "PDF could not be opened.")
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        completed = 0
        try:
            if document.needs_pass and not document.authenticate(password or ""):
                code = (
                    FindingCode.INVALID_PASSWORD
                    if password
                    else FindingCode.PASSWORD_REQUIRED
                )
                return self._failure(
                    code, "PDF could not be opened with the supplied password."
                )
            if not document.page_count:
                return self._failure(
                    FindingCode.NO_PAGES, "PDF contains no usable pages."
                )
            for index in range(document.page_count):
                try:
                    document[index].set_rotation(0)  # In-memory copy only.
                    chunks = pymupdf4llm.to_markdown(
                        document, pages=[index], **PyMuPdf4LlmTextReader._NATIVE
                    )
                    # page_chunks=True gives one dict per page; accept plain text too.
                    pieces = (
                        [chunks]
                        if isinstance(chunks, str)
                        else [
                            str(chunk.get("text", ""))
                            for chunk in chunks
                            if isinstance(chunk, dict)
                        ]
                    )
                    text = "\n".join(
                        line.rstrip() for piece in pieces for line in piece.splitlines()
                    ).strip()
                except (
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    AttributeError,
                    OSError,
                ):
                    findings.append(
                        PyMuPdf4LlmTextReader._finding(
                            FindingCode.PAGE_UNREADABLE,
                            "This PDF page could not be converted.",
                            page_index=index,
                        )
                    )
                    continue
                completed += 1
                if text:
                    blocks.append(
                        TextBlock(
                            text=text,
                            page_index=index,
                            content_format=TextContentFormat.MARKDOWN,
                        )
                    )
                else:
                    findings.append(
                        PyMuPdf4LlmTextReader._finding(
                            FindingCode.EMPTY,
                            "No stored text found on this page; it may still contain "
                            "visible text in pixels (OCR is off).",
                            page_index=index,
                        )
                    )
        finally:
            document.close()
        incomplete = any(
            f.code not in PyMuPdf4LlmTextReader._INFORMATIONAL for f in findings
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
    def _finding(
        code: FindingCode, message: str, *, page_index: int | None = None
    ) -> TextReadFinding:
        return TextReadFinding(code=code, message=message, page_index=page_index)
