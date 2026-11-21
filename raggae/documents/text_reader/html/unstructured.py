"""Unstructured's HTML wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import text_of


class UnstructuredHtmlTextReader(UnstructuredReader):
    """HTML via ``partition_html``; scripts are not run and nothing is fetched."""

    _name, _label = (
        "text_reader_unstructured_html",
        "UnstructuredHtmlTextReader",
    )

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.html import partition_html

        return partition_html(text=text_of(source))
