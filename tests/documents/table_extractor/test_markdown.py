"""Pipe tables in a Markdown document."""

import sys

import pytest

from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    HtmlTableExtractor,
    MarkdownTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout


def extract(markdown, **options):
    data = markdown.encode() if isinstance(markdown, str) else markdown
    return MarkdownTableExtractor().extract(data, **options)


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def rows(table):
    found = {}
    for cell in table.cells:
        found.setdefault(cell.row, []).append(cell.text)
    return [found[row] for row in sorted(found)]


PAGE = """# Prices

Prices are in EUR.

| Part | Price (**EUR**) | Since |
|:-----|------:|:-----:|
| [Lamp head](https://example.com/head) | 110 | 2026-01-02 |
| Stand | `45.50` | 2025-06-01 |
"""


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import markdown

    assert markdown.MarkdownTableExtractor is MarkdownTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = PAGE.encode()
    path = tmp_path / "prices.md"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = MarkdownTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_markdown"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    # Link labels, code as written, emphasis markers removed; GFM always has a header row.
    assert rows(table) == [
        ["Part", "Price (EUR)", "Since"],
        ["Lamp head", "110", "2026-01-02"],
        ["Stand", "45.50", "2025-06-01"],
    ]
    assert [c.header for c in table.cells] == [True] * 3 + [False] * 6
    assert (table.row_count, table.column_count, table.page_index) == (3, 3, 0)
    assert (table.method, table.region_id, table.caption) == ("declared", None, None)
    assert all(c.row_span == c.column_span == 1 for c in table.cells)
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_the_markdown_and_html_forms_read_back_into_the_same_cells():
    (table,) = extract(PAGE).tables
    (from_markdown,) = extract(table.to_markdown()).tables
    assert from_markdown.cells == table.cells
    (from_html,) = HtmlTableExtractor().extract(table.to_html().encode()).tables
    assert from_html.cells == table.cells


def test_cell_text_is_what_the_markdown_text_reader_reads():
    table = extract(
        "| A | B | C |\n|---|---|---|\n"
        "| ![pump](pump.png) | a \\| b | ~~old~~ *new* |\n"
    ).tables[0]
    assert rows(table)[1] == ["pump", "a | b", "old new"]


def test_short_rows_are_not_padded_and_extra_cells_are_dropped():
    result = extract(
        "| A | B | C |\n|---|---|---|\n| short |\n| one | two | three | four |\n| | | |\n"
    )
    (table,) = result.tables
    assert [(c.row, c.column) for c in table.cells if c.row == 1] == [(1, 0)]
    assert rows(table)[2] == ["one", "two", "three"]
    # A row of empty cells, written as such, keeps its cells.
    assert rows(table)[3] == ["", "", ""]
    assert result.outcome == "partial" and codes(result) == [("fields_dropped", 0)]


def test_raw_html_cells_and_html_tables_are_not_read():
    result = extract(
        "| A | B |\n|---|---|\n| a<br>b | c |\n\n"
        "<div>no table here</div>\n\n<TABLE><tr><td>html</td></tr></TABLE>\n"
    )
    assert rows(result.tables[0])[1] == ["", "c"]
    assert result.outcome == "partial" and codes(result) == [("html_omitted", 0)]
    plain = extract("<div>no table here</div>\n\n| A |\n|---|\n| a |\n")
    assert plain.outcome == "success" and not plain.findings


def test_tables_in_lists_and_quotes_and_not_in_code():
    markdown = (
        "- item\n\n  | A | B |\n  |---|---|\n  | a | b | extra |\n\n"
        "> | Q | R |\n> |---|---|\n> | q | r | extra |\n\n"
        "```\n| not | a table |\n|---|---|\n```\n"
    )
    result = extract(markdown)
    assert [rows(table) for table in result.tables] == [
        [["A", "B"], ["a", "b"]],
        [["Q", "R"], ["q", "r"]],
    ]
    assert codes(result) == [("fields_dropped", 0)]


def test_windows_line_endings_keep_the_rows_lined_up():
    result = extract("| A | B |\r\n|---|---|\r\n| a |\r\n| b | c |\r\n")
    assert rows(result.tables[0]) == [["A", "B"], ["a"], ["b", "c"]]
    assert not result.findings


def test_documents_without_tables_and_tables_without_text():
    for markdown in ("Words only.", ""):
        result = extract(markdown)
        assert result.outcome == "success" and codes(result) == [("empty", 0)]
    result = extract("| |\n|---|\n| |\n")
    assert not result.tables and codes(result) == [("empty_table", 0)]


def test_encoding_is_configured_never_guessed():
    data = "| Café |\n|---|\n| x |\n".encode("cp1252")
    table = MarkdownTableExtractor(encoding="cp1252").extract(data).tables[0]
    assert table.cells[0].text == "Café"
    failed = extract(data)
    assert failed.outcome == "failed" and codes(failed) == [("decode_failed", None)]


@pytest.mark.parametrize("encoding", [None, 1, "latin-9"])
def test_encoding_option_is_validated(encoding):
    with pytest.raises((TypeError, ValueError)):
        MarkdownTableExtractor(encoding=encoding)


def test_missing_file_and_missing_markdown_it(tmp_path, monkeypatch):
    assert codes(extract(tmp_path / "missing.md")) == [("source_unreadable", None)]
    monkeypatch.setitem(sys.modules, "markdown_it", None)
    with pytest.raises(ImportError, match=r"raggae\[text-markdown\]"):
        extract(PAGE)


def test_layouts_are_refused_before_the_source_is_opened(tmp_path):
    missing = tmp_path / "missing.md"
    layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = MarkdownTableExtractor().extract(missing, layout=layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "grid")
    ]
    empty = MarkdownTableExtractor().extract(missing, layout=layout, region_ids=[])
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]
