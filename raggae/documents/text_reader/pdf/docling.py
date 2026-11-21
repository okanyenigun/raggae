"""Read PDF document elements with Docling, offline, from local models."""

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
    TextReadMethod,
    TextReadResult,
)
from ..utils.sources import failure, prepare_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class DoclingPdfTextReader:
    """Return one block per Docling document element, with its page.

    Docling's conversion pipeline (layout and TableFormer models) finds
    headings, paragraphs, list items, captions, page headers/footers, and
    tables, in Docling's reading order. Every content layer is read (body,
    headers/footers, background, invisible text such as an existing OCR
    layer, notes), not only Docling's default body layer. Each element becomes a block with the
    ``page_index`` of its first provenance; tables are Markdown, everything
    else plain text. The pipeline runs offline from ``models_path`` (a folder
    from Docling's model downloader, e.g. ``~/.cache/raggae/docling``) and never
    downloads. OCR, picture classification/description, and remote services are
    off; pictures are reported, not read. Layout regions are unsupported.
    Requires the opt-in ``raggae[text-docling]`` extra.
    """

    def __init__(self, *, models_path: Path | str) -> None:
        if not isinstance(models_path, (Path, str)):
            raise TypeError("models_path must be a path to Docling's local models")
        path = Path(models_path).expanduser()
        if not path.is_dir():
            raise ValueError("models_path must be an existing folder of Docling models")
        self._models_path = path.resolve()
        self._converter: Any = None

    _method: TextReadMethod = "native"
    _pictures_message = (
        "This page has pictures; their contents were not read (OCR is off)."
    )
    _empty_message = (
        "No stored text found on this page; it may still contain visible text "
        "in pixels (OCR is off)."
    )

    @property
    def name(self) -> str:
        return "text_reader_docling_pdf"

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
            unsupported_message=f"{type(self).__name__} reads whole documents, not layout regions.",
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
                    self._finding(
                        FindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        converter = self._load()
        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        opened = self._decrypted(data, prepared.password)
        if isinstance(opened, TextReadResult):
            return opened
        try:
            result = converter.convert(
                DocumentStream(name="source.pdf", stream=BytesIO(opened)),
                raises_on_error=False,
            )
        except (RuntimeError, ValueError, TypeError, OSError):
            return self._failure(
                FindingCode.DECODE_FAILED, "Docling could not convert the PDF."
            )
        if result.status not in (
            ConversionStatus.SUCCESS,
            ConversionStatus.PARTIAL_SUCCESS,
        ):
            return self._failure(
                FindingCode.DECODE_FAILED, "Docling could not convert the PDF."
            )
        blocks, findings = self._elements(result)
        informational = {
            code for code in (FindingCode.EMPTY, FindingCode.PICTURES_UNREAD)
        }
        incomplete = any(f.code not in informational for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if blocks else "failed") if incomplete else "success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _elements(self, result: Any) -> tuple[list[TextBlock], list[TextReadFinding]]:
        """Blocks and findings for a converted document; partial success noted."""
        from docling.datamodel.base_models import ConversionStatus

        blocks, findings = self._document_elements(result.document)
        if result.status == ConversionStatus.PARTIAL_SUCCESS:
            findings.append(
                self._finding(
                    FindingCode.CONVERSION_PARTIAL,
                    "Docling reported errors on part of the document; some content may be missing.",
                )
            )
        return blocks, findings

    def _load(self) -> Any:
        """Build Docling's PDF converter once per reader; models load here."""
        if self._converter is not None:
            return self._converter
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as error:
            raise ImportError(
                "Docling PDF reading requires: pip install 'raggae[text-docling]'"
            ) from error
        options = PdfPipelineOptions(
            artifacts_path=self._models_path,
            do_ocr=False,
            do_table_structure=True,
            do_picture_classification=False,
            do_picture_description=False,
            generate_page_images=False,
            generate_picture_images=False,
            enable_remote_services=False,
        )
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )
        try:
            converter.initialize_pipeline(InputFormat.PDF)
        except (OSError, RuntimeError, ValueError, KeyError, ImportError) as error:
            raise ValueError(
                "models_path does not contain usable Docling layout and table models; "
                "download them with docling.utils.model_downloader.download_models"
            ) from error
        self._converter = converter
        return converter

    def _decrypted(self, data: bytes, password: str | None) -> bytes | TextReadResult:
        """Apply an Artifact password with pikepdf; Docling gets an open PDF."""
        import pikepdf

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with pikepdf.Pdf.open(
                    BytesIO(data), password=password or ""
                ) as document:
                    if not len(document.pages):
                        return self._failure(
                            FindingCode.NO_PAGES, "PDF contains no usable pages."
                        )
                    if not document.is_encrypted:
                        return data
                    with BytesIO() as output:
                        document.save(output)
                        return output.getvalue()
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

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    def _finding(
        self, code: FindingCode, message: str, *, page_index: int | None = None
    ) -> TextReadFinding:
        return TextReadFinding(
            code=code,
            message=message,
            page_index=page_index,
        )

    def _document_elements(
        self, document: Any
    ) -> tuple[list[TextBlock], list[TextReadFinding]]:
        """Docling elements in reading order; empty pages and pictures reported."""
        # Public API; docling_core re-exports these without declaring __all__.
        from docling_core.types.doc import (
            ContentLayer,  # pyright: ignore[reportPrivateImportUsage]
            PictureItem,  # pyright: ignore[reportPrivateImportUsage]
            TableItem,  # pyright: ignore[reportPrivateImportUsage]
            TextItem,  # pyright: ignore[reportPrivateImportUsage]
        )

        blocks: list[TextBlock] = []
        used: set[int] = set()
        pictures: set[int] = set()
        # Every layer (body, headers/footers, background, invisible, notes): Docling's
        # default is body only, which would drop headers and an existing OCR layer.
        for item, _ in document.iterate_items(
            included_content_layers=set(ContentLayer)
        ):
            page = item.prov[0].page_no - 1 if item.prov else 0
            if isinstance(item, PictureItem):
                pictures.add(page)
                continue
            if isinstance(item, TableItem):
                text, kind = (
                    item.export_to_markdown(doc=document).strip(),
                    TextContentFormat.MARKDOWN,
                )
            elif isinstance(item, TextItem):
                text, kind = item.text.strip(), TextContentFormat.PLAIN_TEXT
            else:
                continue
            if text:
                blocks.append(
                    TextBlock(
                        text=text,
                        page_index=page,
                        content_format=kind,
                        method=self._method,
                    )
                )
                used.add(page)
        findings: list[TextReadFinding] = []
        for number in sorted(document.pages):
            page = number - 1
            if page in pictures:
                findings.append(
                    self._finding(
                        FindingCode.PICTURES_UNREAD,
                        self._pictures_message,
                        page_index=page,
                    )
                )
            if page not in used:
                findings.append(
                    self._finding(
                        FindingCode.EMPTY, self._empty_message, page_index=page
                    )
                )
        return blocks, findings
