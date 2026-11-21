"""Read each CSV row as labelled plain text, keeping every field a string."""

from collections.abc import Sequence
from pathlib import Path
from .utils import (
    CsvParseError,
    column_label,
    empty_finding,
    mismatch_finding,
    parse_finding,
    parsed_rows,
    require_delimiter,
    require_header,
    stopped,
)
from ..models import TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import prepare_source, read_source_text, require_encoding
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class CsvTextReader:
    """Return one plain-text block per logical CSV row, one ``label: value`` per line.

    With ``header=True`` (the default), the first nonblank record supplies the
    labels and produces no block; blank header cells and every column of a
    headerless file are labelled ``Column N``. A row whose field count differs
    from the header, or malformed quoting, ends the read: earlier rows are
    kept, that row and later rows are not returned. Values are never
    trimmed, typed, or reformatted, so ``00123`` stays ``00123``. A quoted value
    containing line breaks stays inside its row's block. No dialect, header, or
    encoding is guessed. Layout-region requests are explicitly unsupported.
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
        return "text_reader_csv"

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
            unsupported_message="CsvTextReader reads whole CSV sources, not layout regions.",
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text

        labels: tuple[str, ...] | None = None
        blocks: list[TextBlock] = []
        for row in parsed_rows(text, delimiter=self._delimiter):
            if isinstance(row, CsvParseError):
                return stopped(self.name, blocks, (parse_finding(row),))
            if self._header and labels is None:
                labels = tuple(
                    name if name.strip() else column_label(index)
                    for index, name in enumerate(row.fields)
                )
                continue
            if labels is not None and len(row.fields) != len(labels):
                return stopped(
                    self.name,
                    blocks,
                    (mismatch_finding(row, len(labels)),),
                )
            blocks.append(
                TextBlock(
                    text="\n".join(
                        f"{labels[index] if labels else column_label(index)}: {value}"
                        for index, value in enumerate(row.fields)
                    ),
                    page_index=0,
                )
            )

        findings: tuple[TextReadFinding, ...] = (
            () if blocks else (empty_finding(header_seen=labels is not None),)
        )
        return TextReadResult(
            reader=self.name, outcome="success", blocks=tuple(blocks), findings=findings
        )
