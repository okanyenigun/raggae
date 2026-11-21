"""Unstructured's plain-text wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import text_of


class UnstructuredTextReader(UnstructuredReader):
    """Plain text via ``partition_text``."""

    _name, _label = (
        "text_reader_unstructured_text",
        "UnstructuredTextReader",
    )

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.text import partition_text

        return partition_text(text=text_of(source))
