import codecs
import csv
import io

import pytest

from raggae.documents.validation import CsvActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("content,rows,sample", [
    pytest.param("=1+1\r\n", [["=1+1"]], "=1+1", id="first-cell-formula"),
    pytest.param('"=SUM(1,2)",plain\r\n', [["=SUM(1,2)", "plain"]], "=SUM(1,2)", id="quoted-first-cell-formula"),
])
@pytest.mark.parametrize("encoding,bom,codec", [
    pytest.param("utf-8", codecs.BOM_UTF8, "utf-8-sig", id="utf8-bom"),
    pytest.param("utf-16-le", codecs.BOM_UTF16_LE, "utf-16", id="utf16-le-bom"),
    pytest.param("utf-16-be", codecs.BOM_UTF16_BE, "utf-16", id="utf16-be-bom"),
])
def test_bom_marked_csv_does_not_hide_formula(file_factory, content, rows, sample, encoding, bom, codec):
    data = bom + content.encode(encoding)
    assert data.decode(codec, errors="strict") == content
    assert list(csv.reader(io.StringIO(data.decode(codec), newline=""))) == rows
    path = file_factory("encoded.csv", data)
    result = CsvActiveContentProbe().check(path, "csv")
    assert result.codes == (Code.SPREADSHEET_FORMULA,)
    assert dict(result.findings[0].detail) == {"count": 1, "sample": sample}
    assert dict(result.facts) == {}
    assert path.read_bytes() == data
