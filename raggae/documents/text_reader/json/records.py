"""Read each array item as one JSON record without flattening its contents."""

from collections.abc import Sequence
from pathlib import Path
from .utils import (
    dump_json,
    failure,
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


class RecordJsonTextReader:
    """Return one complete item per block from the selected JSON array.

    The default selects a root array. array_pointer selects a nested array,
    such as /orders. No field-name guessing or automatic object-to-array routing.
    """

    def __init__(
        self, *, array_pointer: str = "", encoding: TextEncoding = "utf-8"
    ) -> None:
        self._encoding: TextEncoding = require_encoding(encoding)
        self._parts = pointer_parts(array_pointer)

    @property
    def name(self) -> str:
        return "text_reader_record_json"

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
        _, document = loaded
        try:
            records = resolve_pointer(document, self._parts)
        except LookupError:
            return failure(
                self.name,
                FindingCode.POINTER_UNRESOLVED,
                "The requested array pointer could not be resolved.",
                page_index=0,
            )
        if not isinstance(records, list):
            return failure(
                self.name,
                FindingCode.STRUCTURE_UNSUPPORTED,
                "The requested value must be a JSON array.",
                page_index=0,
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(
                TextBlock(
                    text=dump_json(value),
                    page_index=0,
                    content_format=TextContentFormat.JSON,
                )
                for value in records
            ),
            findings=()
            if records
            else (
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="The selected JSON array contains no records.",
                    page_index=0,
                ),
            ),
        )
