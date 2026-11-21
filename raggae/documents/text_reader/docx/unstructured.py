"""Unstructured's Word wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import binary_file


class UnstructuredDocxTextReader(UnstructuredReader):
    """Word via ``partition_docx``."""

    _name, _label = (
        "text_reader_unstructured_docx",
        "UnstructuredDocxTextReader",
    )
    _binary = True

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.docx import partition_docx

        return partition_docx(file=binary_file(source))
