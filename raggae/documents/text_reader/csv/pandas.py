"""Read CSV with pandas, one labelled row per block, with values kept as text."""

import io
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from ..utils.sources import failure, prepare_source, read_source_text, require_encoding
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.pandas_tables import (
    CSV_OPTIONS,
    allowed_options,
    column_labels,
    is_positional,
    row_text,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class PandasCsvTextReader:
    """Return one ``label: value`` block per row, parsed by ``pandas.read_csv``.

    An alternative to ``CsvTextReader`` for pandas users: ``options`` accepts
    an allow-listed set of ``read_csv`` options (``sep``, ``skiprows``,
    ``usecols``, ``comment``, ``on_bad_lines``, ...). Values always stay exact
    strings and are never turned into missing values. Rows follow pandas'
    rules: short rows are padded with empty values; extra fields that pandas
    drops are reported and make the result ``partial``. Layout regions are
    unsupported.
    """

    # Locked: values stay strings, nothing becomes NaN, no hidden index column.
    _FORCED = {
        "dtype": str,
        "keep_default_na": False,
        "na_filter": False,
        "index_col": False,
    }

    def __init__(
        self,
        *,
        options: Mapping[str, Any] | None = None,
        encoding: TextEncoding = "utf-8",
    ) -> None:
        self._options = allowed_options(options, CSV_OPTIONS)
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_pandas_csv"

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
            unsupported_message="PandasCsvTextReader reads whole tables, not layout regions.",
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text
        try:
            import pandas
            from pandas.errors import EmptyDataError, ParserError, ParserWarning
        except ImportError as error:
            raise ImportError(
                "pandas CSV reading requires: pip install 'raggae[text-pandas]'"
            ) from error
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", ParserWarning)
                frame = pandas.read_csv(
                    io.StringIO(text), **self._options, **PandasCsvTextReader._FORCED
                )
        except EmptyDataError:
            return PandasCsvTextReader._empty("Source contains no CSV rows.")
        except (ParserError, ValueError, TypeError, IndexError, UnicodeError):
            return failure(
                self.name,
                FindingCode.PARSE_FAILED,
                "pandas could not parse the source with these options.",
            )
        labels = column_labels(frame, positional=is_positional(frame, self._options))
        blocks = [
            TextBlock(
                text=row_text(
                    labels, ["" if value is None else str(value) for value in row]
                ),
                page_index=0,
            )
            for row in frame.itertuples(index=False, name=None)
        ]
        findings: list[TextReadFinding] = []
        dropped = any(issubclass(item.category, ParserWarning) for item in captured)
        if dropped:
            findings.append(
                TextReadFinding(
                    code=FindingCode.FIELDS_DROPPED,
                    message=(
                        "pandas reported rows with more fields than the header; "
                        "their extra fields were dropped."
                    ),
                    page_index=0,
                )
            )
        if not blocks:
            findings.append(
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="Source contains a header but no data rows.",
                    page_index=0,
                )
            )
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if blocks else "failed") if dropped else "success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    @staticmethod
    def _empty(message: str) -> TextReadResult:
        return TextReadResult(
            reader="text_reader_pandas_csv",
            outcome="success",
            findings=(
                TextReadFinding(code=FindingCode.EMPTY, message=message, page_index=0),
            ),
        )
