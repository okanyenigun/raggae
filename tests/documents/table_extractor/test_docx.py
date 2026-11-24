"""Tables declared in a Word document's main body, standalone or by layout region."""

import sys
from io import BytesIO
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches
from PIL import Image

from raggae.documents.layout_analysis import LayoutAnalysisResult, LayoutRegion
from raggae.documents.layout_analysis.models import DocxElementLocation
from raggae.documents.layout_analysis.region_discovery.docx import DocxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    DocxTableExtractor,
    HtmlTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout

NS = nsdecls("w", "wp", "a", "r")
WPS = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'


def save(document):
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def extract(document, **options):
    data = document if isinstance(document, bytes) else save(document)
    return DocxTableExtractor().extract(data, **options)


def codes(result):
    return [(f.code, f.region_id) for f in result.findings]


def grid(table):
    return [
        (c.row, c.column, c.row_span, c.column_span, c.text, c.header)
        for c in table.cells
    ]


def with_body(*elements):
    """A document whose body holds these raw elements before sectPr."""
    document = Document()
    body = document.element.body
    for element in elements:
        body.insert(len(body) - 1, parse_xml(element))
    return document


def run(text):
    return f"<w:r><w:t xml:space='preserve'>{text}</w:t></w:r>"


def cell(inner="", properties=""):
    return f"<w:tc>{f'<w:tcPr>{properties}</w:tcPr>' if properties else ''}<w:p>{inner}</w:p></w:tc>"


def table_xml(*rows, grid_columns=None, properties=""):
    columns = "".join("<w:gridCol w:w='1000'/>" for _ in range(grid_columns or 0))
    return (
        f"<w:tbl {NS} {WPS}><w:tblPr>{properties}</w:tblPr>"
        f"<w:tblGrid>{columns}</w:tblGrid>{''.join(rows)}</w:tbl>"
    )


def row(*cells, properties=""):
    return f"<w:tr>{f'<w:trPr>{properties}</w:trPr>' if properties else ''}{''.join(cells)}</w:tr>"


def png():
    buffer = BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def prices():
    """A heading and a price table: "Part" merged down two rows, "Price" across two
    columns, the first two rows repeated as header rows, a cell with two paragraphs."""
    document = Document()
    document.add_paragraph("Price list")
    table = document.add_table(rows=4, cols=3)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "Part"
    table.cell(0, 1).merge(table.cell(0, 2)).text = "Price"
    table.cell(1, 1).text, table.cell(1, 2).text = "2025", "2026"
    for column, value in enumerate(("Motor", "110", "120")):
        table.cell(2, column).text = value
    stand = table.cell(3, 0)
    stand.text = "Stand"
    stand.add_paragraph("with base plate")
    table.cell(3, 1).text = "45.50"
    for header in table.rows[:2]:
        header._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    return document


PRICES = [
    (0, 0, 2, 1, "Part", True),
    (0, 1, 1, 2, "Price", True),
    (1, 1, 1, 1, "2025", True),
    (1, 2, 1, 1, "2026", True),
    (2, 0, 1, 1, "Motor", False),
    (2, 1, 1, 1, "110", False),
    (2, 2, 1, 1, "120", False),
    (3, 0, 1, 1, "Stand\nwith base plate", False),
    (3, 1, 1, 1, "45.50", False),
    (3, 2, 1, 1, "", False),
]


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import docx

    assert docx.DocxTableExtractor is DocxTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = save(prices())
    path = tmp_path / "prices.docx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = DocxTableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_docx"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    assert grid(table) == PRICES
    assert (table.row_count, table.column_count) == (4, 3)
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "declared",
        0,
        None,
        None,
    )
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_the_html_form_reads_back_into_the_same_cells():
    (table,) = extract(prices()).tables
    (again,) = HtmlTableExtractor().extract(table.to_html().encode()).tables
    assert again.cells == table.cells
    assert (again.row_count, again.column_count) == (table.row_count, table.column_count)


# The grid ------------------------------------------------------------------------------


def test_header_rows_are_the_leading_rows_word_repeats():
    marker = "<w:tblHeader/>"
    document = with_body(
        table_xml(
            row(cell(run("A")), properties=marker),
            row(cell(run("B")), properties="<w:tblHeader w:val='0'/>"),
            row(cell(run("C")), properties=marker),  # not leading: Word ignores it
        )
    )
    (table,) = extract(document).tables
    assert [(c.text, c.header) for c in table.cells] == [
        ("A", True),
        ("B", False),
        ("C", False),
    ]


def test_grid_before_and_the_declared_grid_width():
    document = with_body(
        table_xml(
            row(cell(run("a")), cell(run("b"))),
            row(cell(run("c")), properties="<w:gridBefore w:val='1'/>"),
            grid_columns=3,
        )
    )
    (table,) = extract(document).tables
    assert [(c.row, c.column, c.text) for c in table.cells] == [
        (0, 0, "a"),
        (0, 1, "b"),
        (1, 1, "c"),
    ]
    assert table.column_count == 3


def test_vertical_merges_continue_only_the_cell_above():
    restart, cont = "<w:vMerge w:val='restart'/>", "<w:vMerge/>"
    document = with_body(
        table_xml(
            row(cell(run("Tall"), restart), cell(run("x"))),
            row(cell(run("more"), cont), cell(run("y"))),
            row(cell(run("lone")), cell(run("z"), cont)),
        )
    )
    (table,) = extract(document).tables
    # Text in a continued cell joins the merged cell. An ordinary cell ends a merge,
    # and a continuation with nothing above to continue ("z") is an ordinary cell.
    assert [(c.row, c.column, c.row_span, c.text) for c in table.cells] == [
        (0, 0, 2, "Tall\nmore"),
        (0, 1, 1, "x"),
        (1, 1, 1, "y"),
        (2, 0, 1, "lone"),
        (2, 1, 1, "z"),
    ]


def test_a_continuation_must_cover_the_same_columns():
    document = with_body(
        table_xml(
            row(cell(run("Wide"), "<w:gridSpan w:val='2'/><w:vMerge w:val='restart'/>")),
            row(cell(run("narrow"), "<w:vMerge/>"), cell(run("b"))),
        )
    )
    (table,) = extract(document).tables
    assert [(c.row, c.column, c.row_span, c.column_span) for c in table.cells] == [
        (0, 0, 1, 2),
        (1, 0, 1, 1),
        (1, 1, 1, 1),
    ]


@pytest.mark.parametrize(("value", "span"), [("2", 2), ("x", 1), ("0", 1), ("99999", 1000)])
def test_grid_spans_are_read_and_capped(value, span):
    document = with_body(table_xml(row(cell(run("a"), f"<w:gridSpan w:val='{value}'/>"))))
    (table,) = extract(document).tables
    assert table.cells[0].column_span == span == table.column_count


def test_captions_come_from_the_alt_text():
    both = with_body(
        table_xml(
            row(cell(run("a"))),
            properties="<w:tblCaption w:val='Prices'/><w:tblDescription w:val='Long text'/>",
        )
    )
    description = with_body(
        table_xml(row(cell(run("a"))), properties="<w:tblDescription w:val='Long text'/>")
    )
    assert extract(both).tables[0].caption == "Prices"
    assert extract(description).tables[0].caption == "Long text"


# Cell text -------------------------------------------------------------------------------


def test_cell_text_is_what_the_word_text_reader_reads():
    document = Document()
    table = document.add_table(rows=1, cols=3)
    first = table.cell(0, 0)
    first.text = "Line one"
    first.add_paragraph("")
    first.add_paragraph("Line two")
    picture = table.cell(0, 1).paragraphs[0]
    picture.add_run("Photo: ")
    picture.add_run().add_picture(png())
    picture._p.xpath(".//wp:docPr")[0].set("descr", "a pump")
    table.cell(0, 2)._tc.remove(table.cell(0, 2).paragraphs[0]._p)
    table.cell(0, 2)._tc.append(
        parse_xml(
            f"<w:p {NS}>{run('kept')}<w:ins w:id='1' w:author='a'>{run(' added')}</w:ins>"
            f"<w:del w:id='2' w:author='a'><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>"
        )
    )
    result = extract(document)
    assert [c.text for c in result.tables[0].cells] == [
        "Line one\nLine two",
        "Photo: a pump",
        "kept added",
    ]
    assert codes(result) == [("revisions_present", None)]


def test_deleted_rows_are_left_out():
    document = Document()
    table = document.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "Kept"
    table.cell(1, 0).text = "Removed"
    table.rows[1]._tr.get_or_add_trPr().append(
        parse_xml(f"<w:del {NS} w:id='1' w:author='a'/>")
    )
    result = extract(document)
    assert [c.text for c in result.tables[0].cells] == ["Kept"]
    assert result.tables[0].row_count == 1
    assert codes(result) == [("revisions_present", None)]


def test_a_nested_table_is_read_into_its_cell():
    document = Document()
    outer = document.add_table(rows=1, cols=2)
    outer.cell(0, 0).text = "Kit"
    inner = outer.cell(0, 1).add_table(rows=2, cols=2)
    for (r, c), value in {(0, 0): "bolt", (0, 1): "4", (1, 0): "nut", (1, 1): "8"}.items():
        inner.cell(r, c).text = value
    result = extract(document)
    (table,) = result.tables
    assert table.cells[1].text == "bolt | 4\nnut | 8"
    assert codes(result) == [("nested_table", None)]


def test_tables_without_text_and_documents_without_tables():
    document = Document()
    document.add_table(rows=2, cols=2)
    result = extract(document)
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty_table", None)]
    plain = Document()
    plain.add_paragraph("Words only.")
    assert codes(extract(plain)) == [("empty", None)]


def test_tables_inside_content_controls_are_found():
    wrapped = (
        f"<w:sdt {NS}><w:sdtPr/><w:sdtContent>"
        + table_xml(row(cell(run("inside"))))
        + "</w:sdtContent></w:sdt>"
    )
    (table,) = extract(with_body(wrapped)).tables
    assert table.cells[0].text == "inside"


def test_tables_in_text_boxes_and_other_parts_are_reported():
    box = (
        f"<w:p {NS} {WPS}><w:r><w:drawing><wp:anchor><wp:docPr id='9' name='tb'/>"
        "<a:graphic><a:graphicData uri='x'><wps:wsp><wps:txbx><w:txbxContent>"
        + table_xml(row(cell(run("boxed"))))
        + "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic>"
        "</wp:anchor></w:drawing></w:r></w:p>"
    )
    document = with_body(box)
    header = document.sections[0].header.add_table(rows=1, cols=1, width=Inches(2))
    header.cell(0, 0).text = "In the header"
    result = extract(document)
    assert not result.tables
    assert codes(result) == [
        ("empty", None),
        ("other_parts_unread", None),
        ("text_boxes_unread", None),
    ]


# Sources -----------------------------------------------------------------------------------


def zip_bytes(entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"private plain text",
        zip_bytes({"word/document.xml": "<x/>"}),
        zip_bytes({"[Content_Types].xml": "<Types/>"}),
    ],
)
def test_non_docx_sources_fail_safely(data):
    result = extract(data)
    assert result.outcome == "failed" and codes(result) == [("decode_failed", None)]
    assert "private" not in result.model_dump_json()


def test_missing_files_and_missing_python_docx(tmp_path, monkeypatch):
    assert codes(DocxTableExtractor().extract(tmp_path / "missing.docx")) == [
        ("source_unreadable", None)
    ]
    data = save(prices())
    monkeypatch.setitem(sys.modules, "docx", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        DocxTableExtractor().extract(data)


def test_too_deep_structure_is_reported(monkeypatch):
    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(DocxTableExtractor, "_tables", staticmethod(too_deep))
    result = extract(prices())
    assert result.outcome == "failed" and codes(result) == [("structure_unsupported", None)]


# Layout regions -----------------------------------------------------------------------------


def with_picture_in_a_cell():
    document = prices()
    paragraph = document.tables[0].cell(2, 0).paragraphs[0]
    paragraph.add_run().add_picture(png())
    paragraph._p.xpath(".//wp:docPr")[0].set("descr", "(photo)")
    return save(document)


def test_layout_links_each_table_to_its_table_region():
    data = save(prices())
    layout = DocxLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    result = extract(data, layout=layout)
    assert [t.region_id for t in result.tables] == [region.id]
    assert grid(result.tables[0]) == PRICES
    assert result.outcome == "success" and not result.findings


def test_explicit_regions_and_a_picture_owning_its_alt_text():
    data = with_picture_in_a_cell()
    layout = DocxLayoutAnalyzer().analyze(data)
    kinds = {r.kind: r.id for r in layout.regions}
    alone = extract(data, layout=layout)
    assert alone.tables[0].cells[4].text == "Motor(photo)"
    result = extract(data, layout=layout, region_ids=[kinds["text"], kinds["table"], kinds["image"]])
    assert [t.region_id for t in result.tables] == [kinds["table"]]
    assert result.tables[0].cells[4].text == "Motor"
    assert codes(result) == [("empty", kinds["text"]), ("empty", kinds["image"])]


def regions(*items):
    data = save(prices())
    layout = DocxLayoutAnalyzer().analyze(data)
    surface = layout.surfaces[0]
    return data, LayoutAnalysisResult(
        outcome="success",
        surfaces=layout.surfaces,
        regions=tuple(
            LayoutRegion(
                id=identifier,
                surface_id=surface.id_,
                kind=kind,
                location=DocxElementLocation(part=part, child_indexes=path),
            )
            for identifier, kind, part, path in items
        ),
    )


def test_unresolved_unsupported_and_overlapping_regions():
    data, layout = regions(
        ("far", "table", "word/document.xml", (9,)),
        ("row", "unknown", "word/document.xml", (1, 2)),
        ("twin-a", "table", "word/document.xml", (1,)),
        ("twin-b", "table", "word/document.xml", (1,)),
    )
    result = extract(data, layout=layout, region_ids=[r.id for r in layout.regions])
    assert result.outcome == "failed" and not result.tables
    assert codes(result) == [
        ("region_unresolved", "far"),
        ("selection_unsupported", "row"),
        ("selection_overlap", "twin-a"),
        ("selection_overlap", "twin-b"),
    ]


def test_non_word_locations_and_empty_selections(tmp_path):
    pdf_layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = extract(prices(), layout=pdf_layout)
    assert result.outcome == "failed" and codes(result) == [("selection_unsupported", "grid")]
    missing = tmp_path / "missing.docx"
    assert codes(DocxTableExtractor().extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
    no_tables = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    result = DocxTableExtractor().extract(missing, layout=no_tables)
    assert result.outcome == "success" and codes(result) == [("nothing_selected", None)]


def test_a_layout_for_another_package_part_is_not_resolved():
    from raggae.documents.layout_analysis import LayoutSurface
    from raggae.documents.layout_analysis.models import DocxBodyLocation

    part = "word/other.xml"
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(LayoutSurface(id_="body", kind="flow", location=DocxBodyLocation(part=part)),),
        regions=(
            LayoutRegion(
                id="elsewhere",
                surface_id="body",
                kind="table",
                location=DocxElementLocation(part=part, child_indexes=(1,)),
            ),
        ),
    )
    result = extract(prices(), layout=layout)
    assert result.outcome == "failed" and codes(result) == [("region_unresolved", "elsewhere")]
