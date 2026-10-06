import csv
import io

import pytest

from raggae.documents.validation import CsvActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


def encode_rows(rows, newline):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator=newline, quoting=csv.QUOTE_ALL)
    writer.writerows(rows)
    content = buffer.getvalue()
    assert list(csv.reader(io.StringIO(content, newline=""))) == rows
    return content.encode("utf-8")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("rows,offenders", [
    pytest.param([["notes,=1+1", "plain\n+SUM(A1:A2)", 'say "hello",@SUM(A1:A2)']], [], id="safe-embedded-prefixes"),
    pytest.param([['"=1+1"', '"+SUM(A1:A2)"', "'=1+1"]], [], id="literal-quotes-and-apostrophe"),
    pytest.param([["", "", "plain"], ["", "=1+1", ""]], ["=1+1"], id="empty-cells"),
    pytest.param([["=SUM(1,2)", '+HYPERLINK("https://evil.test/file")']],
                 ["=SUM(1,2)", '+HYPERLINK("https://evil.test/file")'], id="complete-formula-samples"),
    pytest.param([["=1+1\n+2", "notes,=2+2"], ["=1+1\n+2", "@SUM(A1:A2)"]],
                 ["=1+1\n+2", "=1+1\n+2", "@SUM(A1:A2)"], id="count-real-multiline-cells"),
    pytest.param([["  =SUM(1,2)  ", " -12.5 ", "  +2e-3  ", "'=1+1"]],
                 ["=SUM(1,2)"], id="trim-without-discarding-escaping"),
])
def test_standard_csv_serialization_preserves_real_cell_boundaries(file_factory, newline, rows, offenders):
    data = encode_rows(rows, newline)
    path = file_factory("serialized.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == ((Code.SPREADSHEET_FORMULA,) if offenders else ())
    assert dict(result.facts) == {}
    if offenders:
        assert dict(result.findings[0].detail) == {
            "count": len(offenders), "sample": ", ".join(value[:60] for value in offenders[:5])}
    assert path.read_bytes() == data


@pytest.mark.parametrize("sample_size", [0, 1, 10])
def test_quoted_cells_keep_sample_limits_and_occurrence_count(file_factory, sample_size):
    formulas = ["=SUM(1,2)", "=1+1\n+2", "=" + "A" * 70, "=SUM(1,2)"]
    data = encode_rows([formulas, ["notes,=1+1"]], "\r\n")
    path = file_factory("sampled.csv", data)
    probe = CsvActiveContentProbe()
    probe.set_referenced_samples(sample_size)
    result = probe.check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {
        "count": 4, "sample": ", ".join(value[:60] for value in formulas[:sample_size])}
    assert path.read_bytes() == data
