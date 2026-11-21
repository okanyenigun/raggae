"""Represent each CSV row as an XML element, keeping every field a string."""

from collections.abc import Sequence
from pathlib import Path
from .utils import (
    CsvParseError,
    CsvRow,
    empty_finding,
    mismatch_finding,
    parse_finding,
    parsed_rows,
    require_delimiter,
    require_header,
    stopped,
    unique_names,
)
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ..utils.sources import failure, prepare_source, read_source_text, require_encoding
from ..utils.xml_text import XmlText
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class CsvXmlTextReader:
    """Return one XML-content block per logical CSV row.

    With ``header=True`` (the default), each row is a ``<row>`` element with one
    child per column, named by the header: ``<row><item>Tape</item></row>``. Header
    names are used verbatim and must be valid XML names; a repeated name gets
    the next free numbered suffix (``item``, ``item_2``) and a finding. A blank or
    invalid name fails the read. With ``header=False``, each value is a
    ``<value>`` child and rows may differ in length. Values are text, never
    typed or trimmed; markup characters are escaped. A row whose field count
    differs from the header, malformed quoting, or a character XML cannot
    represent ends the read: earlier rows are kept. No dialect, header, or
    encoding is guessed. Layout regions are unsupported.
    """

    def __init__(
        self,
        *,
        header: bool = True,
        delimiter: str = ",",
        encoding: TextEncoding = "utf-8",
    ) -> None:
        self._header = require_header(header)
        self._delimiter = require_delimiter(delimiter)
        self._encoding: TextEncoding = require_encoding(encoding)
        self._xml = XmlText()

    @property
    def name(self) -> str:
        return "text_reader_csv_xml"

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
                "CsvXmlTextReader reads whole CSV sources, not layout regions."
            ),
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text

        tags: tuple[str, ...] | None = None
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        for row in parsed_rows(text, delimiter=self._delimiter):
            if isinstance(row, CsvParseError):
                findings.append(parse_finding(row))
                return stopped(self.name, blocks, findings)
            if self._header and tags is None:
                for index, name in enumerate(row.fields):
                    if not name.strip():
                        return failure(
                            self.name,
                            FindingCode.HEADER_BLANK,
                            f"Header column {index + 1} has no name; "
                            "no rows were returned.",
                            page_index=0,
                        )
                    if not self._xml.is_name(name):
                        return failure(
                            self.name,
                            FindingCode.HEADER_INVALID,
                            f"Header column {index + 1} is not a valid XML tag "
                            "name; no rows were returned.",
                            page_index=0,
                        )
                tags, renamed = unique_names(row.fields)
                findings.extend(
                    TextReadFinding(
                        code=FindingCode.HEADER_RENAMED,
                        message=(
                            f"Header column {index + 1} repeats an earlier column "
                            "name; its tag was given a numbered suffix."
                        ),
                        page_index=0,
                    )
                    for index in renamed
                )
                continue
            if tags is not None and len(row.fields) != len(tags):
                findings.append(mismatch_finding(row, len(tags)))
                return stopped(self.name, blocks, findings)
            if any(self._xml.has_illegal(value) for value in row.fields):
                findings.append(self._illegal_finding(row))
                return stopped(self.name, blocks, findings)
            names = tags if tags is not None else ("value",) * len(row.fields)
            blocks.append(
                TextBlock(
                    text="<row>"
                    + "".join(
                        self._xml.element(name, value)
                        for name, value in zip(names, row.fields)
                    )
                    + "</row>",
                    page_index=0,
                    content_format=TextContentFormat.XML,
                )
            )

        if not blocks:
            findings.append(empty_finding(header_seen=tags is not None))
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    @staticmethod
    def _illegal_finding(row: CsvRow) -> TextReadFinding:
        return TextReadFinding(
            code=FindingCode.INVALID_CHARACTER,
            message=(
                f"Row starting on line {row.line} holds a character XML cannot "
                "represent; it and later rows were not returned."
            ),
            page_index=0,
        )
