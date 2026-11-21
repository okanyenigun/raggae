"""Represent each CSV row as JSON content, keeping every field a string."""

import json
from collections.abc import Sequence
from pathlib import Path
from .utils import (
    CsvParseError,
    empty_finding,
    mismatch_finding,
    parse_finding,
    parsed_rows,
    require_delimiter,
    require_header,
    stopped,
    unique_names,
)
from ..utils.sources import failure, prepare_source, read_source_text, require_encoding
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class CsvJsonTextReader:
    """Return one JSON-content block per logical CSV row.

    With ``header=True`` (the default), each row is an object keyed by the
    header names in column order: ``{"item":"Tape","quantity":"12"}``. Header
    names are used verbatim; a repeated name gets the next free numbered suffix
    (``amount``, ``amount_2``) and a finding, so no value is overwritten. A blank
    header name fails the read. With ``header=False``, each row is an array of
    strings and rows may differ in length. Values are never typed, so ``00123``
    stays the string ``"00123"``. A row whose field count differs from the
    header, or malformed quoting, ends the read: earlier rows are kept. No
    dialect, header, or encoding is guessed. Layout regions are unsupported.
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

    @property
    def name(self) -> str:
        return "text_reader_csv_json"

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
                "CsvJsonTextReader reads whole CSV sources, not layout regions."
            ),
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text

        keys: tuple[str, ...] | None = None
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        for row in parsed_rows(text, delimiter=self._delimiter):
            if isinstance(row, CsvParseError):
                findings.append(parse_finding(row))
                return stopped(self.name, blocks, findings)
            if self._header and keys is None:
                for index, name in enumerate(row.fields):
                    if not name.strip():
                        return failure(
                            self.name,
                            FindingCode.HEADER_BLANK,
                            f"Header column {index + 1} has no name; "
                            "no rows were returned.",
                            page_index=0,
                        )
                keys, renamed = unique_names(row.fields)
                findings.extend(
                    TextReadFinding(
                        code=FindingCode.HEADER_RENAMED,
                        message=(
                            f"Header column {index + 1} repeats an earlier column "
                            "name; its key was given a numbered suffix."
                        ),
                        page_index=0,
                    )
                    for index in renamed
                )
                continue
            if keys is not None and len(row.fields) != len(keys):
                findings.append(mismatch_finding(row, len(keys)))
                return stopped(self.name, blocks, findings)
            content = list(row.fields) if keys is None else dict(zip(keys, row.fields))
            blocks.append(
                TextBlock(
                    text=json.dumps(content, separators=(",", ":")),
                    page_index=0,
                    content_format=TextContentFormat.JSON,
                )
            )

        if not blocks:
            findings.append(empty_finding(header_seen=keys is not None))
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )
