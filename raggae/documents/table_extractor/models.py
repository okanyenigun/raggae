"""Extracted tables and extraction findings shared by document table extractors."""

import json
import re
from enum import StrEnum
from html import escape as html_escape
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from xml.sax.saxutils import escape as xml_escape
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..shared.csv_text import unique_names
from ..text_reader.utils.xml_text import XmlText

if TYPE_CHECKING:
    from pandas import DataFrame

type TableExtractOutcome = Literal["success", "partial", "failed", "not_applicable"]
type TableExtractMethod = Literal["declared", "ruled", "aligned", "model"]

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class TableFindingCode(StrEnum):
    """Why an extraction was incomplete, empty, or limited; the same meaning in every extractor."""

    # Source and decoding (same meaning as the text-reader codes of the same name)
    SOURCE_UNREADABLE = (
        "source_unreadable"  # Missing, unreadable, or not a regular file.
    )
    DECODE_FAILED = "decode_failed"  # Bytes could not be decoded or the file opened.
    PARSE_FAILED = "parse_failed"  # Decoded, but the content's syntax is invalid.
    NOT_APPLICABLE = "not_applicable"  # Not a format this extractor handles.
    PASSWORD_REQUIRED = "password_required"  # Encrypted; no password supplied.
    INVALID_PASSWORD = "invalid_password"  # Encrypted; the supplied password is wrong.
    NO_PAGES = "no_pages"  # The document has no usable pages.
    EMPTY = "empty"  # Extracted successfully, but no tables in this scope.

    # Selections and layout regions
    SELECTION_UNSUPPORTED = (
        "selection_unsupported"  # Extractor or region cannot be read by region.
    )
    NOTHING_SELECTED = (
        "nothing_selected"  # An empty selection, or a layout with no table regions.
    )
    SELECTION_OVERLAP = (
        "selection_overlap"  # Selected regions locate the same element; neither read.
    )
    REGION_UNRESOLVED = (
        "region_unresolved"  # Region's location not found in the source.
    )
    LAYOUT_MISMATCH = "layout_mismatch"  # Layout dimensions do not match the source.
    PAGE_MISSING = "page_missing"  # Requested page does not exist.

    # Pages
    PAGE_UNREADABLE = "page_unreadable"  # One page's tables could not be read.
    OCR_FAILED = "ocr_failed"  # The OCR service failed on a page or region.
    CONVERSION_PARTIAL = (
        "conversion_partial"  # Backend reported errors on part of the document.
    )

    # Document structure
    HIDDEN_SKIPPED = "hidden_skipped"  # Hidden elements were not read.
    UNSUPPORTED_CONTENT = (
        "unsupported_content"  # Content of an unsupported kind was skipped.
    )
    STRUCTURE_UNSUPPORTED = (
        "structure_unsupported"  # Document structure this extractor cannot map.
    )
    REVISIONS_PRESENT = (
        "revisions_present"  # Tracked changes exist; accepted text was read.
    )
    OTHER_PARTS_UNREAD = (
        "other_parts_unread"  # Headers, footers, notes, or comments not read.
    )
    TEXT_BOXES_UNREAD = "text_boxes_unread"  # Text boxes were not read.
    HIDDEN_SLIDE = "hidden_slide"  # A hidden slide was read.
    HIDDEN_CONTENT = "hidden_content"  # Hidden content was read and is included.
    FORMULA_RESULT_MISSING = "formula_result_missing"  # A formula has no saved result.
    BACKEND_COVERAGE = "backend_coverage"  # The backend may not cover all content.
    HTML_OMITTED = "html_omitted"  # Raw HTML inside Markdown was left out.
    FIELDS_DROPPED = "fields_dropped"  # The parser dropped fields from a row.
    ROW_LENGTH_MISMATCH = (
        "row_length_mismatch"  # A row's field count differs from the header.
    )
    FIELD_TOO_LARGE = "field_too_large"  # A field exceeds the parser's size limit.

    # Tables
    EMPTY_TABLE = "empty_table"  # A table with no text in any cell; not returned.
    NESTED_TABLE = (
        "nested_table"  # A table inside a cell was read as text in that cell.
    )
    SPAN_SHORTENED = (
        "span_shortened"  # A cell's span ran into another cell and was shortened.
    )
    CELLS_OUTSIDE_TABLES = (
        "cells_outside_tables"  # Filled cells outside declared tables were not read.
    )


class TableCell(BaseModel):
    """One cell, listed once at its top-left grid position, with how far it spans."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: int = Field(
        ge=0,
        strict=True,
        description="Zero-based row of the cell's top-left grid position.",
        examples=[0, 2],
    )
    column: int = Field(
        ge=0,
        strict=True,
        description="Zero-based column of the cell's top-left grid position.",
        examples=[0, 1],
    )
    row_span: int = Field(
        default=1,
        ge=1,
        strict=True,
        description="How many rows the cell covers, starting at row.",
        examples=[1, 2],
    )
    column_span: int = Field(
        default=1,
        ge=1,
        strict=True,
        description="How many columns the cell covers, starting at column.",
        examples=[1, 2],
    )
    text: str = Field(
        description='The cell\'s text as the format\'s text reader reads it; "" for an empty cell. A line break inside the cell is "\\n". Never a typed value.',
        examples=["Motor", "", "Line one\nLine two"],
    )
    header: bool = Field(
        default=False,
        strict=True,
        description="True only when the source marks the cell as a header (an HTML <th>, a declared header row); never inferred from how it looks.",
        examples=[False, True],
    )


class ExtractedTable(BaseModel):
    """One table as a grid of cells, with its source page and optional layout-region ID.

    The cells are the table. The ``to_*`` methods give the same table as HTML
    (which keeps everything), Markdown, a pandas DataFrame, JSON, or XML.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cells: tuple[TableCell, ...] = Field(
        min_length=1,
        description="Each cell once, at its top-left position, listed row by row and left to right. A grid position no cell covers is a short row, not padding.",
    )
    row_count: int = Field(
        ge=1,
        strict=True,
        description="Rows in the grid, empty rows included.",
        examples=[3],
    )
    column_count: int = Field(
        ge=1,
        strict=True,
        description="Columns in the grid: the widest row, spans included.",
        examples=[3],
    )
    page_index: int = Field(
        default=0,
        ge=0,
        strict=True,
        description="Zero-based source page, slide, or sheet where the table starts. Documents without pages use 0. Geometry belongs to layout analysis.",
        examples=[0, 3],
    )
    region_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="Input layout region that produced this table, or None for standalone whole-document extraction.",
        examples=[None, "html-r4"],
    )
    method: TableExtractMethod = Field(
        description="How the grid was obtained. declared: the format states it; ruled: inferred from drawn lines; aligned: inferred from text positions; model: predicted by a trained model.",
        examples=["declared", "ruled"],
    )
    caption: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="A caption or title the author attached to the table, as stored; None when there is none. Never text found near the table.",
        examples=[None, "Prices in 2026"],
    )

    @model_validator(mode="after")
    def _grid(self) -> Self:
        anchors = [(cell.row, cell.column) for cell in self.cells]
        if any(left >= right for left, right in zip(anchors, anchors[1:])):
            raise ValueError("cells must be listed row by row, each position once")
        covered: dict[int, list[tuple[int, int]]] = {}
        for cell in self.cells:
            if (
                cell.row + cell.row_span > self.row_count
                or cell.column + cell.column_span > self.column_count
            ):
                raise ValueError("cells must fit inside the grid")
            for row in range(cell.row, cell.row + cell.row_span):
                covered.setdefault(row, []).append(
                    (cell.column, cell.column + cell.column_span)
                )
        for spans in covered.values():
            spans.sort()
            if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
                raise ValueError("no two cells may cover the same position")
        if not any(cell.text.strip() for cell in self.cells):
            raise ValueError("a table needs text in at least one cell")
        return self

    # The row-based forms (Markdown, DataFrame, JSON, XML) share one rule for column
    # labels and one for values, so they always agree with each other:
    # - Header rows are the leading rows where every covered position belongs to a
    #   header cell. A column's label is the texts of the header cells above it,
    #   top to bottom, joined with " / " (a header spanning columns names each of
    #   them; one spanning header rows counts once). A column left without text, or
    #   every column of a table without header rows, is "Column N". A repeated
    #   label gets the CSV readers' numbered suffix ("amount", "amount_2").
    # - Data rows are the rows after the header rows. A cell's text appears once,
    #   at its top-left position; a position where no cell starts has no value
    #   (None, null, no element, or empty in Markdown).

    def _coverage(self) -> dict[tuple[int, int], TableCell]:
        """The cell covering each covered (row, column) position."""
        owner: dict[tuple[int, int], TableCell] = {}
        for cell in self.cells:
            for row in range(cell.row, cell.row + cell.row_span):
                for column in range(cell.column, cell.column + cell.column_span):
                    owner[(row, column)] = cell
        return owner

    def _header_row_count(self) -> int:
        """Leading rows in which every covered position belongs to a header cell."""
        owner = self._coverage()
        count = 0
        for row in range(self.row_count):
            cells = [
                cell
                for column in range(self.column_count)
                if (cell := owner.get((row, column))) is not None
            ]
            if not cells or not all(cell.header for cell in cells):
                break
            count += 1
        return count

    def _column_labels(self) -> tuple[str, ...]:
        """One unique label per column, shared by the row-based forms."""
        owner = self._coverage()
        rows = self._header_row_count()
        labels: list[str] = []
        for column in range(self.column_count):
            texts: list[str] = []
            seen: set[tuple[int, int]] = set()
            for row in range(rows):
                cell = owner.get((row, column))
                if cell is None or (cell.row, cell.column) in seen:
                    continue
                seen.add((cell.row, cell.column))
                if cell.text.strip():
                    texts.append(cell.text)
            labels.append(" / ".join(texts) or f"Column {column + 1}")
        return unique_names(labels)[0]

    def _data_rows(self) -> list[list[str | None]]:
        """The rows after the header rows: each cell's text where it starts, else None."""
        starts = {(cell.row, cell.column): cell.text for cell in self.cells}
        return [
            [starts.get((row, column)) for column in range(self.column_count)]
            for row in range(self._header_row_count(), self.row_count)
        ]

    def to_html(self) -> str:
        """The table as HTML: rowspan, colspan, <th>, and <caption> kept; nothing lost."""
        owner = self._coverage()
        # Positions after a row's last covered one are left out (a short row); an
        # uncovered position before it needs an empty cell to keep the grid.
        ends: dict[int, int] = {}
        for row, column in owner:
            ends[row] = max(ends.get(row, 0), column + 1)
        lines = ["<table>"]
        if self.caption is not None:
            lines.append(f"<caption>{self._html_text(self.caption)}</caption>")
        for row in range(self.row_count):
            cells: list[str] = []
            for column in range(ends.get(row, 0)):
                cell = owner.get((row, column))
                if cell is None:
                    cells.append("<td></td>")
                elif (cell.row, cell.column) == (row, column):
                    tag = "th" if cell.header else "td"
                    spans = (
                        f' rowspan="{cell.row_span}"' if cell.row_span > 1 else ""
                    ) + (
                        f' colspan="{cell.column_span}"' if cell.column_span > 1 else ""
                    )
                    cells.append(f"<{tag}{spans}>{self._html_text(cell.text)}</{tag}>")
            lines.append(f"<tr>{''.join(cells)}</tr>")
        lines.append("</table>")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """The table as a GFM pipe table: one header row of column labels; spans flattened."""
        labels = self._column_labels()
        lines = [
            self._markdown_row(labels),
            "|" + "|".join("---" for _ in labels) + "|",
            *(
                self._markdown_row(["" if value is None else value for value in row])
                for row in self._data_rows()
            ),
        ]
        return "\n".join(lines)

    def to_dataframe(self) -> "DataFrame":
        """The table as a pandas DataFrame of strings, columns named by the column labels.

        Needs pandas: pip install 'raggae[text-pandas]'.
        """
        try:
            import pandas
        except ImportError as error:
            raise ImportError(
                "Table DataFrames require: pip install 'raggae[text-pandas]'"
            ) from error
        return pandas.DataFrame(
            self._data_rows(),
            columns=pandas.Index(self._column_labels(), dtype=object),
            dtype=object,
        )

    def to_json(self) -> str:
        """The table as JSON text: one object per data row, keyed by the column labels."""
        labels = self._column_labels()
        records = [dict(zip(labels, row)) for row in self._data_rows()]
        return json.dumps(records, separators=(",", ":"))

    def to_xml(self) -> str:
        """The table as XML text: one <row> per data row, each value a <cell column="...">.

        Raises ValueError when the text holds a character XML 1.0 cannot represent.
        """
        xml = XmlText()
        labels = self._column_labels()
        rows = self._data_rows()
        texts = [self.caption or "", *labels, *(v for row in rows for v in row if v)]
        if any(xml.has_illegal(text) for text in texts):
            raise ValueError("the table holds a character XML 1.0 cannot represent")
        lines = ["<table>"]
        if self.caption is not None:
            lines.append(f"  {xml.element('caption', self.caption)}")
        for row in rows:
            cells = "".join(
                f"<cell{xml.attribute('column', label)}/>"
                if not value
                else f"<cell{xml.attribute('column', label)}>{self._xml_text(value)}</cell>"
                for label, value in zip(labels, row)
                if value is not None
            )
            lines.append(f"  <row>{cells}</row>" if cells else "  <row/>")
        lines.append("</table>")
        return "\n".join(lines)

    @staticmethod
    def _html_text(text: str) -> str:
        """Escaped text; a line break becomes <br> so it survives HTML whitespace rules."""
        return _LINE_BREAK.sub("<br>", html_escape(text, quote=False))

    @staticmethod
    def _xml_text(text: str) -> str:
        return xml_escape(text, {"\r": "&#13;"})

    @staticmethod
    def _markdown_row(values: Any) -> str:
        return (
            "| " + " | ".join(ExtractedTable._markdown_text(v) for v in values) + " |"
        )

    @staticmethod
    def _markdown_text(text: str) -> str:
        """One line, as the text readers write a table cell, with ``|`` escaped."""
        line = " ".join(part.strip() for part in text.splitlines() if part.strip())
        return line.replace("|", "\\|")


class TableExtractFinding(BaseModel):
    """A safe explanation of an empty extraction, unsupported selection, or failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: TableFindingCode = Field(
        description="Stable machine-readable reason from TableFindingCode; the extractor is named on the result.",
        examples=["empty_table", "selection_unsupported"],
    )
    message: str = Field(
        min_length=1,
        description="Short explanation without raw backend errors or source excerpts.",
        examples=["A table with no text in any cell was not returned."],
    )
    region_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="Affected input layout region, when this finding concerns a selection.",
        examples=[None, "html-r4"],
    )
    page_index: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Affected zero-based page index, or None when no specific page is known.",
        examples=[None, 0, 1],
    )


class TableExtractResult(BaseModel):
    """One extractor's tables and coverage findings; never a changed file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extractor: str = Field(
        min_length=1,
        description="Name of the worker that performed this extraction.",
        examples=["table_extractor_html"],
    )
    outcome: TableExtractOutcome = Field(
        description="Success means the requested scope completed; partial means some work is missing; failed means none completed; not_applicable means an unsupported format.",
        examples=["success", "partial", "failed", "not_applicable"],
    )
    tables: tuple[ExtractedTable, ...] = Field(
        default=(),
        description="Tables with page indexes in extraction sequence, not a guarantee of reading order. A document without tables has none.",
    )
    findings: tuple[TableExtractFinding, ...] = Field(
        default=(),
        description="Explanations of empty extractions, unsupported selections, failures, and limits.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome in ("failed", "not_applicable") and self.tables:
            raise ValueError("failed/not-applicable results cannot contain tables")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
