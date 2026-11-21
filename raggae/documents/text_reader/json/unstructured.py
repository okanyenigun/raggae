"""Unstructured's JSON wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import text_of


class UnstructuredJsonTextReader(UnstructuredReader):
    """JSON via ``partition_json``.

    Arbitrary JSON becomes one text element (the pretty-printed document). A
    file that is Unstructured's own serialized element list is turned back
    into those elements: that is Unstructured's behavior.
    """

    _name, _label = (
        "text_reader_unstructured_json",
        "UnstructuredJsonTextReader",
    )

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.json import partition_json

        return partition_json(text=text_of(source))
