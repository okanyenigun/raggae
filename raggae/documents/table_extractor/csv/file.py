"""The table a CSV file is, read with the standard library."""

from collections.abc import Sequence
from pathlib import Path
from typing import get_args
from ..models import (
    ExtractedTable,
    TableCell,
    TableExtractFinding,
    TableExtractResult,
    TableFindingCode,
)
from ..utils import failure, finding, finish, prepare_source, read_source
from ...shared.csv_text import (
    CsvParseError,
    CsvRow,
    parsed_rows,
    require_delimiter,
    require_header,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class CsvTableExtractor:
    """Return the table a CSV file is, read with the standard library.

    The whole file is one ``ExtractedTable``: each record a row, in source
    order (blank lines skipped), each field a cell, with ``method="declared"``
    and ``page_index=0``. With ``header=True`` (the default) the first record
    holds header cells; ``header=False`` declares none. Fields are read as
    ``CsvTextReader`` reads them: strings, never trimmed, typed, or
    reformatted, so ``00123`` stays ``00123``; a quoted field keeps its line
    breaks.

    A row with a different number of fields than the header keeps the fields
    written: a short row is not padded and a long one widens the grid. It is
    reported (``row_length_mismatch``), since its values may sit in the wrong
    columns. Malformed quoting ends the read (``parse_failed``), as does a field
    above the ``csv`` module's size limit (``field_too_large``): the rows before
    it are kept. A file without fields is ``empty``; one whose fields are all
    empty, ``empty_table``. No dialect, header, or encoding is guessed. CSV has
    no layout regions, so any layout is refused before the source is opened.
    """

    _INFORMATIONAL = frozenset(
        {
            TableFindingCode.EMPTY,
            TableFindingCode.EMPTY_TABLE,
            TableFindingCode.ROW_LENGTH_MISMATCH,
        }
    )

    def __init__(
        self,
        *,
        header: bool = True,
        delimiter: str = ",",
        encoding: TextEncoding = "utf-8",
    ) -> None:
        self._header = require_header(header)
        self._delimiter = require_delimiter(delimiter)
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "table_extractor_csv"

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
            unsupported_message="CSV has no layout regions; this extractor reads "
            "whole files.",
        )
        if isinstance(prepared, TableExtractResult):
            return prepared
        data = read_source(prepared, self.name, allow_empty=True)
        if isinstance(data, TableExtractResult):
            return data
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return failure(
                self.name,
                TableFindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )
        rows: list[CsvRow] = []
        findings: list[TableExtractFinding] = []
        mismatched: CsvRow | None = None
        for row in parsed_rows(text, delimiter=self._delimiter):
            if isinstance(row, CsvParseError):
                findings.append(CsvTableExtractor._stopped(row))
                break
            if (
                self._header
                and rows
                and mismatched is None
                and len(row.fields) != len(rows[0].fields)
            ):
                mismatched = row
            rows.append(row)
        if mismatched is not None:
            findings.insert(0, CsvTableExtractor._mismatch(mismatched, rows[0]))
        if not rows:
            if findings:  # Parsing stopped at the first record: nothing was read.
                return finish(self.name, [], findings, 0, self._INFORMATIONAL)
            empty = finding(TableFindingCode.EMPTY, "Source contains no CSV rows.", 0)
            return finish(self.name, [], [empty], 1, self._INFORMATIONAL)
        cells = [
            TableCell(
                row=number,
                column=column,
                text=value,
                header=self._header and number == 0,
            )
            for number, row in enumerate(rows)
            for column, value in enumerate(row.fields)
        ]
        if not any(cell.text.strip() for cell in cells):
            findings.append(
                finding(
                    TableFindingCode.EMPTY_TABLE,
                    "A table with no text in any cell was not returned.",
                    0,
                )
            )
            return finish(self.name, [], findings, 1, self._INFORMATIONAL)
        table = ExtractedTable(
            cells=tuple(cells),
            row_count=len(rows),
            column_count=max(len(row.fields) for row in rows),
            page_index=0,
            method="declared",
        )
        return finish(self.name, [table], findings, 1, self._INFORMATIONAL)

    @staticmethod
    def _mismatch(row: CsvRow, header: CsvRow) -> TableExtractFinding:
        def fields(count: int) -> str:
            return f"{count} field" if count == 1 else f"{count} fields"

        return finding(
            TableFindingCode.ROW_LENGTH_MISMATCH,
            f"Row starting on line {row.line} has {fields(len(row.fields))}; the "
            f"header has {fields(len(header.fields))}. Rows that differ keep the "
            "fields written, which may sit in the wrong columns.",
            0,
        )

    @staticmethod
    def _stopped(error: CsvParseError) -> TableExtractFinding:
        if error.field_too_large:
            return finding(
                TableFindingCode.FIELD_TOO_LARGE,
                f"Row starting on line {error.line} has a field larger than Python's "
                "csv field size limit; it and later rows were not read.",
                0,
            )
        return finding(
            TableFindingCode.PARSE_FAILED,
            f"Row starting on line {error.line} could not be parsed as CSV; it and "
            "later rows were not read.",
            0,
        )
