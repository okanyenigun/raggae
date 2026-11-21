"""Unstructured's PowerPoint wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import binary_file


class UnstructuredPptxTextReader(UnstructuredReader):
    """PowerPoint via ``partition_pptx``; ``page_index`` is the slide position."""

    _name, _label = (
        "text_reader_unstructured_pptx",
        "UnstructuredPptxTextReader",
    )
    _binary = True

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.pptx import partition_pptx

        return partition_pptx(file=binary_file(source))
