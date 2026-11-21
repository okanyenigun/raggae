"""Unstructured's JSON Lines wrapper."""

from ...models import TextReadResult
from ...unstructured_base import UnstructuredReader
from ...utils.unstructured_text import lines_result, text_of


class UnstructuredJsonLinesTextReader(UnstructuredReader):
    """JSON Lines: ``partition_json`` per record, keeping the source line.

    Blocks are JSON content ``{"line": n, "value": "<element text>"}`` with
    the original one-based line, as in ``JsonLinesTextReader``. Invalid or
    blank lines are reported; other lines are kept (``partial``).
    """

    _name, _label = (
        "text_reader_unstructured_jsonl",
        "UnstructuredJsonLinesTextReader",
    )

    def _convert(self, loaded: str | bytes) -> TextReadResult:
        from unstructured.partition.json import partition_json

        return lines_result(
            self.name,
            text_of(loaded),
            lambda line: partition_json(text=line),
        )
