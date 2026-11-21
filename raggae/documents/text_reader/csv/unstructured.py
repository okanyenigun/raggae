"""Unstructured's CSV wrapper."""

from io import BytesIO
from typing import Any
from ..unstructured_base import UnstructuredReader
from ..utils.unstructured_text import text_of


class UnstructuredCsvTextReader(UnstructuredReader):
    """CSV via ``partition_csv``: one table element, written as rows.

    Unstructured detects the delimiter itself.
    """

    _name, _label = (
        "text_reader_unstructured_csv",
        "UnstructuredCsvTextReader",
    )

    def _partition(self, source: str | bytes) -> list[Any]:
        from unstructured.partition.csv import partition_csv

        return partition_csv(file=BytesIO(text_of(source).encode("utf-8")))
