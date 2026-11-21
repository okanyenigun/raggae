"""Unstructured's Markdown wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import text_of


class UnstructuredMarkdownTextReader(UnstructuredReader):
    """Markdown via ``partition_md``."""

    _name, _label = (
        "text_reader_unstructured_markdown",
        "UnstructuredMarkdownTextReader",
    )

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.md import partition_md

        return partition_md(text=text_of(source))
