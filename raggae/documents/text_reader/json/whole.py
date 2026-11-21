"""Read a complete validated JSON document as one JSON-content block."""

from collections.abc import Sequence
from pathlib import Path
from .utils import load_document, prepare_source, require_encoding
from ..models import TextBlock, TextContentFormat, TextReadResult
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class WholeJsonTextReader:
    """Preserve the complete decoded source after strict JSON validation."""

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_whole_json"

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
        text, _ = loaded
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=(
                TextBlock(
                    text=text, page_index=0, content_format=TextContentFormat.JSON
                ),
            ),
        )
