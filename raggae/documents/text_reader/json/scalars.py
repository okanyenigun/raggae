"""Read JSON scalars as JSON path/value content without changing the source."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from .utils import dump_json, load_document, prepare_source, require_encoding
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


class JsonTextReader:
    """Return one JSON-content block per scalar, with its JSON Pointer path."""

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_json"

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
        loaded = load_document(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(loaded, TextReadResult):
            return loaded
        _, value = loaded
        blocks: list[TextBlock] = []
        pending: list[tuple[str, Any]] = [("", value)]
        while pending:
            pointer, current = pending.pop()
            if isinstance(current, dict):
                for key, child in reversed(list(current.items())):
                    segment = key.replace("~", "~0").replace("/", "~1")
                    pending.append((f"{pointer}/{segment}", child))
            elif isinstance(current, list):
                pending.extend(
                    (f"{pointer}/{index}", current[index])
                    for index in range(len(current) - 1, -1, -1)
                )
            else:
                payload = (
                    '{"path": '
                    + json.dumps(pointer)
                    + ', "value": '
                    + dump_json(current)
                    + "}"
                )
                blocks.append(
                    TextBlock(
                        text=payload,
                        page_index=0,
                        content_format=TextContentFormat.JSON,
                    )
                )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=()
            if blocks
            else (
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="JSON contains no scalar values; empty containers produce no blocks.",
                    page_index=0,
                ),
            ),
        )
