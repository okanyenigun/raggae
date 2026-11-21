"""Apply a trusted local jq filter independently to each JSON Lines record."""

from collections.abc import Sequence
from pathlib import Path
from .utils import finish_read, parsed_lines, record_block
from ..jq import JqJsonTextReader
from ..utils import prepare_source, read_source_text, require_encoding
from ...models import TextBlock, TextReadFinding, TextReadResult
from ....layout_analysis.models import LayoutAnalysisResult
from ....normalization.artifacts import Artifact
from ....normalization.models import TextEncoding


class JqJsonLinesTextReader:
    """Return one {line, value} block per jq result, retaining the source line.

    Reuses the JSON jq reader's local process contract. Filters are trusted
    application configuration, not sandboxed. Each valid source record gets a
    separate invocation, so a record failure cannot erase another line's output.
    """

    def __init__(
        self,
        expression: str,
        *,
        encoding: TextEncoding = "utf-8",
        executable: Path | str | None = None,
    ) -> None:
        self._encoding: TextEncoding = require_encoding(encoding)
        self._query = JqJsonTextReader(expression, executable=executable)

    @property
    def name(self) -> str:
        return "text_reader_jq_jsonl"

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
        completed = 0
        incomplete = False
        for record in parsed_lines(text):
            if isinstance(record, TextReadFinding):
                findings.append(record)
                incomplete = True
                continue
            line_number, line, _ = record
            result = self._query.read(line.encode("utf-8"))
            if result.outcome == "success":
                completed += 1
                blocks.extend(
                    record_block(line_number, block.text) for block in result.blocks
                )
            else:
                incomplete = True
            findings.extend(
                TextReadFinding(
                    code=finding.code,
                    message=f"Line {line_number}: {finding.message}",
                    page_index=0,
                )
                for finding in result.findings
            )
        return finish_read(
            reader=self.name,
            blocks=blocks,
            findings=findings,
            completed=completed,
            incomplete=incomplete,
        )
