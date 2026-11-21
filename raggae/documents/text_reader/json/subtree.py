"""Read explicitly selected JSON subtrees as independent JSON-content blocks."""

from collections.abc import Sequence
from pathlib import Path
from .utils import (
    dump_json,
    load_document,
    pointer_parts,
    prepare_source,
    require_encoding,
    resolve_pointer,
)
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


class SubtreeJsonTextReader:
    """Return one complete JSON value for each requested JSON Pointer.

    Selections may be objects, arrays, or scalars. Duplicate/ancestor selections
    are caller errors, avoiding duplicate source content. Missing selections are
    findings; independent resolved selections remain available as partial output.
    """

    def __init__(
        self, *, pointers: Sequence[str], encoding: TextEncoding = "utf-8"
    ) -> None:
        self._encoding: TextEncoding = require_encoding(encoding)
        if isinstance(pointers, (str, bytes)) or not isinstance(pointers, Sequence):
            raise TypeError("pointers must be a sequence of JSON Pointer strings")
        parsed = []
        for pointer in pointers:
            parts = pointer_parts(pointer)
            if any(
                parts[: len(other)] == other or other[: len(parts)] == parts
                for other in parsed
            ):
                raise ValueError("pointers must not duplicate or overlap")
            parsed.append(parts)
        self._parts = tuple(parsed)

    @property
    def name(self) -> str:
        return "text_reader_subtree_json"

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
        if not self._parts:
            return TextReadResult(reader=self.name, outcome="success")
        loaded = load_document(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(loaded, TextReadResult):
            return loaded
        _, document = loaded
        blocks = []
        findings = []
        for index, parts in enumerate(self._parts):
            try:
                value = resolve_pointer(document, parts)
            except LookupError:
                findings.append(
                    TextReadFinding(
                        code=FindingCode.POINTER_UNRESOLVED,
                        message=f"Requested pointer at index {index} could not be resolved.",
                        page_index=0,
                    )
                )
            else:
                blocks.append(
                    TextBlock(
                        text=dump_json(value),
                        page_index=0,
                        content_format=TextContentFormat.JSON,
                    )
                )
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if blocks else "failed") if findings else "success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )
