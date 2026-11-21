"""Read Excel sheets with pandas, one labelled row per block."""

import io
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from .book import is_worksheet
from .values import cell_text
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.pandas_tables import (
    EXCEL_OPTIONS,
    allowed_options,
    column_labels,
    is_positional,
    row_text,
)
from ..utils.sources import failure, prepare_source
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class PandasExcelTextReader:
    """Return one ``label: value`` block per row of each sheet, via ``pandas.read_excel``.

    A header-labelled view of worksheets for pandas users: by default every
    worksheet is read with its first row as labels; ``options`` accepts an
    allow-listed set of ``read_excel`` options (``sheet_name``, ``header``,
    ``usecols``, ``skiprows``, ...). ``page_index`` is the sheet's position.
    Values are written as ``XlsxTextReader`` writes them (stored numbers,
    ISO 8601 dates, ``TRUE``/``FALSE``). Formulas give saved results; missing
    ones are reported. Chart sheets and layout regions are unsupported.
    """

    # Raw cell values (formatted like XlsxTextReader), nothing becomes NaN.
    _FORCED = {
        "dtype": object,
        "keep_default_na": False,
        "na_filter": False,
        "engine": "openpyxl",
    }

    def __init__(self, *, options: Mapping[str, Any] | None = None) -> None:
        self._options = allowed_options(options, EXCEL_OPTIONS)

    @property
    def name(self) -> str:
        return "text_reader_pandas_excel"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        prepared = prepare_source(
            source,
            reader=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message=(
                "PandasExcelTextReader reads whole sheets, not layout regions; "
                "use XlsxTextReader for cell-range reads."
            ),
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        try:
            if prepared.path is not None and not S_ISREG(prepared.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = prepared.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except BadZipFile:
            return self._failure(
                FindingCode.DECODE_FAILED, "Source is not a readable workbook package."
            )
        if len(set(names)) != len(names) or "[Content_Types].xml" not in names:
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source is not a readable Office document package.",
            )
        try:
            import pandas
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError as error:
            raise ImportError(
                "pandas Excel reading requires: pip install 'raggae[text-pandas]'"
            ) from error

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            try:
                formulas = load_workbook(
                    io.BytesIO(data), data_only=False, keep_links=False
                )
                cached = load_workbook(
                    io.BytesIO(data), data_only=True, keep_links=False
                )
            except (
                BadZipFile,
                InvalidFileException,
                SyntaxError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                OSError,
            ):
                return self._failure(
                    FindingCode.DECODE_FAILED,
                    "Source could not be opened as a workbook.",
                )
            try:
                sheet_names: list[str] = list(formulas.sheetnames)
                worksheets = [n for n in sheet_names if is_worksheet(formulas[n])]
                requested = self._options.get("sheet_name", worksheets)
                options = {**self._options, "sheet_name": requested}
                try:
                    frames = pandas.read_excel(
                        io.BytesIO(data), **options, **PandasExcelTextReader._FORCED
                    )
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    return self._failure(
                        FindingCode.PARSE_FAILED,
                        "pandas could not read the workbook with these options.",
                    )
                if not isinstance(frames, dict):
                    frames = {
                        PandasExcelTextReader._sheet(requested, sheet_names): frames
                    }
                findings: list[TextReadFinding] = [
                    PandasExcelTextReader._finding(
                        FindingCode.UNSUPPORTED_CONTENT,
                        "This sheet holds a chart, not cells; it was not read.",
                        page_index=index,
                    )
                    for index, name in enumerate(sheet_names)
                    if name not in worksheets and "sheet_name" not in self._options
                ]
                blocks: list[TextBlock] = []
                for key, frame in frames.items():
                    name = PandasExcelTextReader._sheet(key, sheet_names)
                    index = sheet_names.index(name)
                    labels = column_labels(
                        frame, positional=is_positional(frame, options)
                    )
                    for row in frame.itertuples(index=False, name=None):
                        blocks.append(
                            TextBlock(
                                text=row_text(
                                    labels,
                                    [
                                        PandasExcelTextReader._value(value)
                                        for value in row
                                    ],
                                ),
                                page_index=index,
                            )
                        )
                    missing = PandasExcelTextReader._missing_results(
                        formulas[name], cached[name]
                    )
                    if missing:
                        cells = "cell has" if missing == 1 else "cells have"
                        findings.append(
                            PandasExcelTextReader._finding(
                                FindingCode.FORMULA_RESULT_MISSING,
                                f"{missing} formula {cells} no saved result on this sheet; "
                                "left empty rather than calculated.",
                                page_index=index,
                            )
                        )
            finally:
                formulas.close()
                cached.close()
        if any(issubclass(item.category, UserWarning) for item in captured):
            findings.append(
                PandasExcelTextReader._finding(
                    FindingCode.BACKEND_COVERAGE,
                    "The workbook backend reported unsupported features; "
                    "some content may be missing.",
                )
            )
        if not blocks:
            findings.append(
                PandasExcelTextReader._finding(
                    FindingCode.EMPTY, "No data rows were found."
                )
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _sheet(key: Any, names: list[str]) -> str:
        """pandas keys sheets by name or by position, as requested."""
        return names[key] if isinstance(key, int) else str(key)

    @staticmethod
    def _value(value: Any) -> str:
        if hasattr(value, "item") and not isinstance(value, str):
            value = value.item()  # numpy scalars
        return cell_text(value)

    @staticmethod
    def _missing_results(formulas: Any, cached: Any) -> int:
        if not is_worksheet(formulas):
            return 0
        values = cached._cells
        return sum(
            1
            for position, cell in formulas._cells.items()
            if cell.data_type == "f"
            and (position not in values or values[position].value is None)
        )

    @staticmethod
    def _finding(
        code: FindingCode, message: str, *, page_index: int | None = None
    ) -> TextReadFinding:
        return TextReadFinding(code=code, message=message, page_index=page_index)
