import csv
import io

import pytest

from raggae.documents.validation import CsvActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("content,rows,offenders", [
    pytest.param('"notes,=1+1"\n', [["notes,=1+1"]], [], id="comma-inside-safe-cell"),
    pytest.param('"notes\n=1+1"\n', [["notes\n=1+1"]], [], id="newline-inside-safe-cell"),
    pytest.param('"say ""hello"",=1+1"\n', [['say "hello",=1+1']], [], id="escaped-quotes-in-safe-cell"),
    pytest.param('=1+1,"plain,words"\n', [["=1+1", "plain,words"]], ["=1+1"], id="separate-real-formula"),
    pytest.param('"=SUM(1,2)"\n', [["=SUM(1,2)"]], ["=SUM(1,2)"], id="comma-inside-formula"),
    pytest.param('"=1+1\n+2"\n', [["=1+1\n+2"]], ["=1+1\n+2"], id="one-multiline-formula-cell"),
])
def test_formulas_are_checked_per_actual_csv_cell(file_factory, content, rows, offenders):
    # Independently verify the intended CSV boundaries, not the worker's splitting.
    assert list(csv.reader(io.StringIO(content, newline=""))) == rows
    data = content.encode("utf-8")
    path = file_factory("quoted.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == ((Code.SPREADSHEET_FORMULA,) if offenders else ())
    assert dict(result.facts) == {}
    if offenders:
        assert dict(result.findings[0].detail) == {
            "count": len(offenders), "sample": ", ".join(value[:60] for value in offenders[:5])}
    assert path.read_bytes() == data
