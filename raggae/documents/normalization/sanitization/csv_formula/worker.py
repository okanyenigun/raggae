"""Opt-in apostrophe prefixing for formula-like cells in comma-separated CSV."""

import csv
import re
from io import StringIO
from typing import BinaryIO, Unpack, overload
from .models import (
    CsvFormulaFinding,
    CsvFormulaIssue,
    CsvFormulaOutcome,
    CsvFormulaReport,
)
from ...io import SharedInputOutputHandler
from ...utils import write_all_bytes
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


# Preserve conventional signed decimal/scientific numbers, not arbitrary float()
# inputs such as +NaN, -Infinity, or Python-only underscores.
_SIGNED_NUMBER = re.compile(r"[+-](?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class CsvFormulaSanitizer:
    """Prefix selected cells; do not execute formulas or infer another CSV dialect."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_csv_formula"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[CsvFormulaReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[CsvFormulaReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[CsvFormulaReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> CsvFormulaReport:
        try:
            text = source.read().decode("utf-8", errors="strict")
            # UTF-8 BOM is a file marker, not part of the first cell's value.
            bom = text.startswith("\ufeff")
            if bom:
                text = text[1:]
            with StringIO(text, newline="") as reader:
                rows = list(csv.reader(reader, dialect="excel", strict=True))
        except (UnicodeDecodeError, csv.Error):
            return self._failure(
                CsvFormulaFinding.DECODE_FAILED,
                "CSV source must be valid UTF-8 and readable comma-separated CSV.",
            )
        count = 0
        for row in rows:
            for index, cell in enumerate(row):
                if self._formula_like(cell):
                    row[index] = "'" + cell
                    count += 1
        if count == 0:
            return CsvFormulaReport(outcome=CsvFormulaOutcome.NO_CHANGE)
        try:
            with StringIO(newline="") as destination:
                csv.writer(destination, dialect="excel", lineterminator="\n").writerows(
                    rows
                )
                rendered = destination.getvalue()
            if bom:
                rendered = "\ufeff" + rendered
            data = rendered.encode("utf-8", errors="strict")
        except (UnicodeEncodeError, csv.Error):
            return self._failure(
                CsvFormulaFinding.ENCODE_FAILED, "Sanitized CSV could not be encoded."
            )
        write_all_bytes(output, data)
        return CsvFormulaReport(outcome=CsvFormulaOutcome.CHANGED, changed_cells=count)

    @staticmethod
    def _failure(code: CsvFormulaFinding, message: str) -> CsvFormulaReport:
        return CsvFormulaReport(
            outcome=CsvFormulaOutcome.FAILED,
            issues=(CsvFormulaIssue(code=code, message=message),),
        )

    @staticmethod
    def _formula_like(cell: str) -> bool:
        if not cell or cell[0] not in "=+-@":
            return False
        if cell[0] in "+-" and _SIGNED_NUMBER.fullmatch(cell) is not None:
            return False
        return True
