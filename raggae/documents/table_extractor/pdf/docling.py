"""Tables Docling's TableFormer model recognizes in a PDF, offline, from local models."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import failure, finding, finish, place_spans, prepare_source, read_source
from ...shared.docling_pdf import decrypted, pdf_converter
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class DoclingTableExtractor:
    """Return the tables Docling recognizes in a PDF, with their cells.

    Docling's layout model finds each table and its TableFormer model predicts
    the rows, columns, merged cells, and header cells: ``method="model"``, a
    grid as the model sees it. Tables come in Docling's reading order, every
    content layer included, each with the ``page_index`` of the page where it
    starts. A cell's ``row_span`` and ``column_span`` are the model's; a span
    past the table or over another cell is shortened (``span_shortened``).
    Docling's column-header and row-header flags make header cells. A cell's
    text is the PDF's stored text Docling assigns to it (OCR is off).
    ``caption`` is None: linking a caption to a table is relationship analysis.

    The pipeline runs offline from ``models_path`` (a folder from Docling's
    model downloader, e.g. ``~/.cache/raggae/docling``) and never downloads,
    exactly as ``DoclingPdfTextReader`` runs it. A table with no text is not
    returned (``empty_table``); a PDF without tables gets ``empty``. When
    Docling reports errors on part of the document, the tables it found are
    returned with ``conversion_partial``. Passwords come from the
    ``Artifact``. Layout regions are unsupported. Requires the opt-in
    ``raggae[text-docling]`` extra.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.SPAN_SHORTENED,
        }
    )

    def __init__(self, *, models_path: Path | str) -> None:
        if not isinstance(models_path, (Path, str)):
            raise TypeError("models_path must be a path to Docling's local models")
        path = Path(models_path).expanduser()
        if not path.is_dir():
            raise ValueError("models_path must be an existing folder of Docling models")
        self._models_path = path.resolve()
        self._converter: Any = None

    @property
    def name(self) -> str:
        return "table_extractor_docling"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TableExtractResult:
        prepared = prepare_source(
            source,
            extractor=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message="DoclingTableExtractor reads whole documents, not "
            "layout regions.",
        )
        if isinstance(prepared, TableExtractResult):
            return prepared
        data = read_source(prepared, self.name)
        if isinstance(data, TableExtractResult):
            return data
        if b"%PDF-" not in data[:1024]:
            return TableExtractResult(
                extractor=self.name,
                outcome="not_applicable",
                findings=(
                    finding(
                        TableFindingCode.NOT_APPLICABLE, "Source has no PDF header."
                    ),
                ),
            )
        converter = self._load()
        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        opened = decrypted(data, prepared.password)
        if not isinstance(opened, bytes):
            code, message = opened
            return failure(self.name, TableFindingCode(code), message)
        try:
            result = converter.convert(
                DocumentStream(name="source.pdf", stream=BytesIO(opened)),
                raises_on_error=False,
            )
        except (RuntimeError, ValueError, TypeError, OSError):
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Docling could not convert the PDF.",
            )
        if result.status not in (
            ConversionStatus.SUCCESS,
            ConversionStatus.PARTIAL_SUCCESS,
        ):
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Docling could not convert the PDF.",
            )
        tables, findings = DoclingTableExtractor._tables(result.document)
        if result.status == ConversionStatus.PARTIAL_SUCCESS:
            findings.append(
                finding(
                    TableFindingCode.CONVERSION_PARTIAL,
                    "Docling reported errors on part of the document; some tables may "
                    "be missing.",
                )
            )
        return finish(self.name, tables, findings, 1, self._INFORMATIONAL)

    def _load(self) -> Any:
        """Build Docling's PDF converter once per extractor; models load here."""
        if self._converter is not None:
            return self._converter
        try:
            converter = pdf_converter(self._models_path)
        except ImportError as error:
            raise ImportError(
                "Docling table extraction requires: pip install 'raggae[text-docling]'"
            ) from error
        self._converter = converter
        return converter

    @staticmethod
    def _tables(
        document: Any,
    ) -> tuple[list[ExtractedTable], list[TableExtractFinding]]:
        """Docling's tables in reading order, every content layer included."""
        # Public API; docling_core re-exports these without declaring __all__.
        from docling_core.types.doc import (
            ContentLayer,  # pyright: ignore[reportPrivateImportUsage]
            TableItem,  # pyright: ignore[reportPrivateImportUsage]
        )

        tables: list[ExtractedTable] = []
        findings: list[TableExtractFinding] = []
        found = False
        for item, _ in document.iterate_items(
            included_content_layers=set(ContentLayer)
        ):
            if not isinstance(item, TableItem):
                continue
            found = True
            page = item.prov[0].page_no - 1 if item.prov else 0
            table, shortened = DoclingTableExtractor._grid(item.data, page)
            if shortened:
                findings.append(
                    finding(
                        TableFindingCode.SPAN_SHORTENED,
                        "A cell's span ran past the table or into another cell and was "
                        "shortened.",
                        page,
                    )
                )
            if table is None:
                findings.append(
                    finding(
                        TableFindingCode.EMPTY_TABLE,
                        "A table with no text in any cell was not returned.",
                        page,
                    )
                )
            else:
                tables.append(table)
        if not found:
            findings.append(
                finding(TableFindingCode.EMPTY, "Docling found no tables in the PDF.")
            )
        return tables, findings

    @staticmethod
    def _grid(data: Any, page: int) -> tuple[ExtractedTable | None, bool]:
        """The model's cells as a grid; None when no cell has text."""
        rows, columns = int(data.num_rows), int(data.num_cols)
        cells: dict[tuple[int, int], Any] = {}
        wanted: dict[tuple[int, int], tuple[int, int]] = {}
        clipped = False
        for cell in data.table_cells:
            anchor = (int(cell.start_row_offset_idx), int(cell.start_col_offset_idx))
            if (
                anchor in cells
                or not 0 <= anchor[0] < rows
                or not 0 <= anchor[1] < columns
            ):
                continue  # A repeated or out-of-grid cell: the first one stands.
            want = (
                max(1, int(cell.end_row_offset_idx) - anchor[0]),
                max(1, int(cell.end_col_offset_idx) - anchor[1]),
            )
            fitted = (min(want[0], rows - anchor[0]), min(want[1], columns - anchor[1]))
            clipped = clipped or fitted != want
            cells[anchor] = cell
            wanted[anchor] = fitted
        spans, shortened = place_spans(wanted)
        if not any(cell.text.strip() for cell in cells.values()):
            return None, shortened or clipped
        return (
            ExtractedTable(
                cells=tuple(
                    TableCell(
                        row=row,
                        column=column,
                        row_span=spans[(row, column)][0],
                        column_span=spans[(row, column)][1],
                        text=cells[(row, column)].text,
                        header=bool(
                            cells[(row, column)].column_header
                            or cells[(row, column)].row_header
                        ),
                    )
                    for row, column in sorted(spans)
                ),
                row_count=rows,
                column_count=columns,
                page_index=page,
                method="model",
            ),
            shortened or clipped,
        )
