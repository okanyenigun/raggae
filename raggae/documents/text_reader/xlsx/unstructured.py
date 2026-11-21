"""Unstructured's Excel wrapper."""

from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import binary_file


class UnstructuredExcelTextReader(UnstructuredReader):
    """Excel via ``partition_xlsx``; ``page_index`` is the sheet position."""

    _name, _label = (
        "text_reader_unstructured_excel",
        "UnstructuredExcelTextReader",
    )
    _binary = True

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.xlsx import partition_xlsx

        return partition_xlsx(file=binary_file(source))
