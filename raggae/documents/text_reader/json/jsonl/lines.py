"""Read complete JSON Lines records while preserving independent valid lines."""

from collections.abc import Sequence
from pathlib import Path
from .utils import finish_read, parsed_lines, record_block
from ..utils import dump_json, prepare_source, read_source_text, require_encoding
from ...models import TextBlock, TextReadFinding, TextReadResult
from ....layout_analysis.models import LayoutAnalysisResult
from ....normalization.artifacts import Artifact
from ....normalization.models import TextEncoding


class JsonLinesTextReader:
    """Return one {line, value} JSON-content block per valid source line.

    Lines are one-based; every block has page_index=0. Invalid or duplicate-key
    records produce findings without discarding valid records on other lines.
    """

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_jsonl"

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
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        for record in parsed_lines(text):
            if isinstance(record, TextReadFinding):
                findings.append(record)
            else:
                line_number, _, value = record
                blocks.append(record_block(line_number, dump_json(value)))
        return finish_read(
            reader=self.name,
            blocks=blocks,
            findings=findings,
            completed=len(blocks),
            incomplete=bool(findings),
        )
