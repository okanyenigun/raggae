"""Word main-body text, read as if tracked changes were accepted."""

import builtins
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from PIL import Image

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.layout_analysis.models import (
    DocxBodyLocation,
    DocxElementLocation,
)
from raggae.documents.layout_analysis.region_discovery.docx import DocxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import DocxTextReader, TextReader, TextReadResult

NS = nsdecls("w", "wp", "a", "pic", "r")
WPS = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
V = 'xmlns:v="urn:schemas-microsoft-com:vml"'
M = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'


def xml(markup):
    return parse_xml(markup)


def save(document):
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def docx(*paragraphs):
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    return document


def with_body(*elements):
    """A document whose body holds these raw elements before sectPr."""
    document = Document()
    body = document.element.body
    for element in elements:
        body.insert(
            len(body) - 1, xml(element) if isinstance(element, str) else element
        )
    return document


def para(inner):
    return f"<w:p {NS} {WPS} {MC} {V} {M}>{inner}</w:p>"


def run(text):
    return f"<w:r><w:t xml:space='preserve'>{text}</w:t></w:r>"


def text_box(text, descr="box"):
    return (
        "<w:r><w:drawing><wp:anchor><wp:docPr id='9' name='tb' "
        f"descr='{descr}'/><a:graphic><a:graphicData uri='x'><wps:wsp><wps:txbx>"
        f"<w:txbxContent><w:p>{run(text)}</w:p></w:txbxContent></wps:txbx>"
        "</wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>"
    )


def png():
    buffer = BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def picture_paragraph(document, before, alt, after):
    paragraph = document.add_paragraph(before)
    paragraph.add_run().add_picture(png())
    if alt is not None:
        paragraph._p.xpath(".//wp:docPr")[0].set("descr", alt)
    paragraph.add_run(after)
    return paragraph


def read(document, **kwargs):
    data = document if isinstance(document, bytes) else save(document)
    return DocxTextReader().read(data, **kwargs)


def texts(result):
    return [block.text for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def by_region(result):
    return {block.region_id: block.text for block in result.blocks}


# Standalone reading -------------------------------------------------------


def test_docx_family_exports_the_same_worker():
    from raggae.documents.text_reader import docx as docx_readers

    assert docx_readers.DocxTextReader is DocxTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = save(docx("Garantie", "Café İstanbul"))
    path = tmp_path / "terms.docx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = DocxTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_docx"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["Garantie", "Café İstanbul"]
    for block in result.blocks:
        assert block.page_index == 0 and block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_one_block_per_nonempty_paragraph_with_exact_text():
    document = Document()
    document.add_heading("Warranty", level=1)
    document.add_paragraph("")
    document.add_paragraph("   ")
    document.add_paragraph("  Indented,  spaced  ")
    document.add_paragraph("Last")
    assert texts(read(document)) == ["Warranty", "  Indented,  spaced  ", "Last"]


def test_tabs_breaks_and_hyphens_inside_runs():
    document = with_body(
        para(
            "<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t><w:cr/>"
            "<w:t>d</w:t><w:br w:type='page'/><w:t>e</w:t><w:noBreakHyphen/>"
            "<w:t>f</w:t><w:softHyphen/><w:t>g</w:t></w:r>"
        )
    )
    assert texts(read(document)) == ["a\tb\nc\nd\ne-fg"]


def test_hyperlinks_and_fields_give_their_visible_text():
    document = with_body(
        para(
            run("See ")
            + f"<w:hyperlink r:id='rId99'>{run('terms')}</w:hyperlink>"
            + run(", dated ")
            + f"<w:fldSimple w:instr='DATE'>{run('2026-11-18')}</w:fldSimple>"
            + run(", page ")
            + "<w:r><w:fldChar w:fldCharType='begin'/></w:r>"
            + "<w:r><w:instrText> PAGE </w:instrText></w:r>"
            + "<w:r><w:fldChar w:fldCharType='separate'/></w:r>"
            + run("3")
            + "<w:r><w:fldChar w:fldCharType='end'/></w:r>"
        )
    )
    result = read(document)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["See terms, dated 2026-11-18, page 3"]


def test_tracked_changes_are_read_as_if_accepted():
    document = with_body(
        para(
            run("Due within ")
            + "<w:del w:id='1' w:author='a'><w:r><w:delText>30</w:delText></w:r></w:del>"
            + f"<w:ins w:id='2' w:author='a'>{run('45')}</w:ins>"
            + run(" days")
            + f"<w:moveFrom w:id='3' w:author='a'>{run(' (old place)')}</w:moveFrom>"
            + f"<w:moveTo w:id='4' w:author='a'>{run('.')}</w:moveTo>"
        ),
        para(
            "<w:del w:id='5' w:author='a'><w:r><w:delText>Gone</w:delText></w:r></w:del>"
        ),
    )
    result = read(document)
    assert result.outcome == "success"
    assert texts(result) == ["Due within 45 days."]
    assert codes(result) == ["revisions_present"]
    assert "30" not in result.model_dump_json()


def test_deleted_table_rows_are_omitted():
    document = Document()
    table = document.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "Kept"
    table.cell(1, 0).text = "Removed"
    table.rows[1]._tr.get_or_add_trPr().append(
        xml(f"<w:del {NS} w:id='1' w:author='a'/>")
    )
    result = read(document)
    assert texts(result) == ["Kept"]
    assert codes(result) == ["revisions_present"]


def test_content_controls_are_read_inline_and_as_body_blocks():
    document = with_body(
        para(
            run("Signed by ")
            + f"<w:sdt><w:sdtPr/><w:sdtContent>{run('Ada')}</w:sdtContent></w:sdt>"
        ),
        f"<w:sdt {NS}><w:sdtPr/><w:sdtContent><w:p>{run('Control one')}</w:p>"
        f"<w:p>{run('Control two')}</w:p></w:sdtContent></w:sdt>",
        f"<w:customXml {NS} w:element='x'><w:p>{run('Custom')}</w:p></w:customXml>",
    )
    result = read(document)
    assert result.outcome == "success" and not result.findings
    assert texts(result) == ["Signed by Ada", "Control one", "Control two", "Custom"]


def test_tables_put_rows_on_lines_and_merged_cells_once():
    document = Document()
    table = document.add_table(rows=4, cols=3)
    for column, label in enumerate(("Part", "Years", "Note")):
        table.cell(0, column).text = label
    table.cell(1, 0).text = "Motor"
    table.cell(1, 1).text = "2"
    table.cell(2, 0).merge(table.cell(2, 2)).text = "All parts"
    cell = table.cell(3, 0)
    cell.text = "First"
    cell.add_paragraph("second  line")
    result = read(document)
    assert texts(result) == [
        "Part | Years | Note\nMotor | 2 | \nAll parts\nFirst second line |  | "
    ]


def test_vertically_merged_cells_are_read_once():
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "Tall"
    table.cell(0, 1).text = "a"
    table.cell(1, 1).text = "b"
    assert texts(read(document)) == ["Tall | a\n | b"]


def test_nested_table_text_stays_in_its_cell_and_empty_tables_give_nothing():
    document = Document()
    outer = document.add_table(rows=1, cols=2)
    outer.cell(0, 0).text = "Outer"
    inner = outer.cell(0, 1).add_table(rows=1, cols=2)
    inner.cell(0, 0).text = "In1"
    inner.cell(0, 1).text = "In2"
    document.add_table(rows=2, cols=2)
    assert texts(read(document)) == ["Outer | In1 | In2"]


def test_image_alt_text_is_read_inline():
    document = Document()
    picture_paragraph(document, "See ", "wiring\n diagram", " below.")
    picture_paragraph(document, "No ", None, "alt.")
    picture_paragraph(document, "", "Standalone view", "")
    assert texts(read(document)) == [
        "See wiring diagram below.",
        "No alt.",
        "Standalone view",
    ]


def test_vml_picture_alt_text_is_read():
    document = with_body(
        para(run("Old ") + "<w:r><w:pict><v:shape alt='legacy logo'/></w:pict></w:r>")
    )
    assert texts(read(document)) == ["Old legacy logo"]


def test_text_boxes_are_skipped_with_a_finding():
    document = with_body(
        para(run("Main text") + text_box("Fragile", descr="should not appear")),
        para(
            "<w:r><w:pict><v:shape><v:textbox><w:txbxContent>"
            f"<w:p>{run('VML box')}</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
        ),
    )
    result = read(document)
    assert result.outcome == "success"
    assert texts(result) == ["Main text"]
    assert codes(result) == ["text_boxes_unread"]
    assert "Fragile" not in result.model_dump_json()


def test_alternate_content_reads_the_choice_once():
    document = with_body(
        para(
            run("Logo: ")
            + "<w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:drawing>"
            "<wp:inline><wp:docPr id='1' name='p' descr='new logo'/></wp:inline>"
            "</w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape alt='old logo'/>"
            "</w:pict></mc:Fallback></mc:AlternateContent></w:r>"
        )
    )
    assert texts(read(document)) == ["Logo: new logo"]


@pytest.mark.parametrize(
    "element",
    [
        para(run("x = ") + "<m:oMath><m:r><m:t>y</m:t></m:r></m:oMath>"),
        para(run("x = ") + "<w:r><w:object><v:shape/></w:object></w:r>"),
        f"<w:altChunk {NS} r:id='rId50'/>",
    ],
)
def test_equations_objects_and_imported_content_are_noted(element):
    document = with_body(para(run("Before")), element)
    result = read(document)
    assert result.outcome == "success"
    assert texts(result)[0] == "Before"
    assert "y" not in texts(result)
    assert codes(result) == ["unsupported_content"]


@pytest.mark.parametrize(
    "reference", ["footnoteReference", "endnoteReference", "commentReference"]
)
def test_references_to_other_parts_are_reported(reference):
    document = with_body(para(run("Clause") + f"<w:r><w:{reference} w:id='1'/></w:r>"))
    result = read(document)
    assert texts(result) == ["Clause"]
    assert codes(result) == ["other_parts_unread"]


def test_header_with_text_is_reported_and_empty_header_is_not():
    document = docx("Body")
    assert not read(document).findings
    document.sections[0].header.paragraphs[0].text = ""
    assert not read(document).findings
    document.sections[0].header.paragraphs[0].text = "Confidential"
    result = read(document)
    assert texts(result) == ["Body"]
    assert codes(result) == ["other_parts_unread"]
    assert "Confidential" not in result.model_dump_json()


def test_list_numbering_is_not_added():
    document = Document()
    document.add_paragraph("First", style="List Number")
    document.add_paragraph("Bullet", style="List Bullet")
    assert texts(read(document)) == ["First", "Bullet"]


def test_empty_document_succeeds_with_an_explanation():
    result = read(Document())
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


def test_repeated_wording_is_not_deduplicated():
    assert texts(read(docx("Same", "Same"))) == ["Same", "Same"]


def test_too_deep_structure_is_reported(monkeypatch):
    import raggae.documents.text_reader.docx.body as module

    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(module.DocxTextReader, "_body_texts", staticmethod(too_deep))
    result = read(docx("x"))
    assert result.outcome == "failed"
    assert codes(result) == ["structure_unsupported"]


# Invalid sources -----------------------------------------------------------


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
    result = DocxTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = DocxTextReader()
    result = reader.read(tmp_path / "private-name.docx")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    assert reader.read(tmp_path).outcome == "failed"
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


def test_missing_backend_has_an_install_hint(monkeypatch):
    data = save(docx("x"))
    real_import = builtins.__import__

    def without_docx(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("no python-docx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_docx)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        DocxTextReader().read(data)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        DocxTextReader().read(BytesIO(save(docx("x"))))


# Layout-guided reading -----------------------------------------------------


@pytest.fixture
def report():
    document = Document()
    document.add_heading("Warranty", level=1)
    document.add_paragraph("Due within 45 days.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Motor"
    table.cell(0, 1).text = "2"
    picture_paragraph(document, "See ", "wiring diagram", " below.")
    body = document.element.body
    body.insert(
        len(body) - 1,
        xml(
            f"<w:sdt {NS}><w:sdtContent><w:p>{run('Control')}</w:p></w:sdtContent></w:sdt>"
        ),
    )
    data = save(document)
    return data, DocxLayoutAnalyzer().analyze(data)


def region(layout, kind, nth=0):
    return [r for r in layout.regions if r.kind == kind][nth]


def test_layout_regions_from_the_word_analyzer_are_read(report):
    data, layout = report
    heading, table = region(layout, "heading"), region(layout, "table")
    result = DocxTextReader().read(
        data, layout=layout, region_ids=[table.id, heading.id]
    )
    assert result.outcome == "success" and not result.findings
    assert [(b.region_id, b.text) for b in result.blocks] == [
        (table.id, "Motor | 2"),
        (heading.id, "Warranty"),
    ]
    assert all(
        b.page_index == 0 and b.content_format == "plain_text" for b in result.blocks
    )


def test_image_region_owns_its_alt_text(report):
    data, layout = report
    image = region(layout, "image")
    paragraph = next(
        r
        for r in layout.regions
        if r.location.child_indexes == image.location.child_indexes[:1]
    )
    both = DocxTextReader().read(
        data, layout=layout, region_ids=[paragraph.id, image.id]
    )
    assert by_region(both) == {paragraph.id: "See  below.", image.id: "wiring diagram"}
    alone = DocxTextReader().read(data, layout=layout, region_ids=[paragraph.id])
    assert by_region(alone) == {paragraph.id: "See wiring diagram below."}


def test_selecting_all_regions_returns_each_text_once(report):
    data, layout = report
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "success"
    joined = "\n".join(texts(result))
    for piece in (
        "Warranty",
        "Due within 45 days.",
        "Motor | 2",
        "wiring diagram",
        "Control",
    ):
        assert joined.count(piece) == 1
    unknown = region(layout, "unknown")
    assert by_region(result)[unknown.id] == "Control"


DOCUMENT_PART = "word/document.xml"


def at(*child_indexes, part=DOCUMENT_PART):
    """One hand-made region location: element path plus its package part."""
    return child_indexes, part


def custom_layout(*locations, part=DOCUMENT_PART):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="body", kind="flow", location=DocxBodyLocation(part=part)
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=f"r{number}",
                surface_id="body",
                kind="text",
                location=DocxElementLocation(part=region_part, child_indexes=indexes),
                native_order=number - 1,
            )
            for number, (indexes, region_part) in enumerate(locations, start=1)
        ),
    )


@pytest.mark.parametrize("location", [at(9), at(0, 99)])
def test_unresolved_locations_are_reported(location):
    data = save(docx("Kept"))
    layout = custom_layout(at(0), location)
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r1": "Kept"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("region_unresolved", "r2")
    ]


def test_layout_for_a_different_package_part_is_not_resolved():
    data = save(docx("Kept"))
    other = "word/other.xml"
    layout = custom_layout(at(0, part=other), part=other)
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("region_unresolved", "r1")
    ]


def test_unsupported_elements_and_text_boxes_are_rejected_as_regions():
    document = with_body(para(run("Main") + text_box("Box")))
    data = save(document)
    # (0,) paragraph, (0, 1, 0) the text-box drawing, (0, 0) a run, (1,) sectPr.
    layout = custom_layout(at(0, 1, 0), at(0, 0), at(1))
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["selection_unsupported"] * 3
    assert result.findings[0].message == "Text boxes are not read."


def test_fallback_representation_is_rejected_as_a_region():
    document = with_body(
        para(
            "<w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:drawing>"
            "<wp:inline><wp:docPr id='1' name='p' descr='new'/></wp:inline></w:drawing>"
            "</mc:Choice><mc:Fallback><w:pict><v:shape alt='old'/></w:pict>"
            "</mc:Fallback></mc:AlternateContent></w:r>"
        )
    )
    data = save(document)
    layout = custom_layout(at(0, 0, 0, 0, 0), at(0, 0, 0, 1, 0))
    result = DocxTextReader().read(data, layout=layout)
    assert by_region(result) == {"r1": "new"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "r2")
    ]


def test_image_inside_a_deleted_revision_is_an_empty_read():
    document = with_body(
        para(
            "<w:del w:id='1' w:author='a'><w:r><w:drawing><wp:inline>"
            "<wp:docPr id='1' name='p' descr='removed'/></wp:inline></w:drawing></w:r></w:del>"
        )
    )
    data = save(document)
    layout = custom_layout(at(0, 0, 0, 0))
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["revisions_present", "empty"]


def test_two_regions_locating_the_same_element_are_both_rejected():
    data = save(docx("Same", "Other"))
    layout = custom_layout(at(0), at(0), at(1))
    result = DocxTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r3": "Other"}
    assert codes(result) == ["selection_overlap"] * 2


def test_non_word_locations_are_rejected():
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=300,
                height=400,
            ),
        ),
        regions=(
            LayoutRegion(
                id="pdf-r1", surface_id="page-1", kind="text", bbox=(0, 0, 20, 20)
            ),
        ),
    )
    result = DocxTextReader().read(save(docx("Whole")), layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "pdf-r1")
    ]


def test_empty_selection_reads_nothing_and_says_so(tmp_path, monkeypatch, report):
    _, layout = report
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = DocxTextReader().read(
        tmp_path / "missing.docx", layout=layout, region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_layout_argument_validation(tmp_path, report):
    _, layout = report
    reader = DocxTextReader()
    source = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=layout, region_ids=["unknown"])
    with pytest.raises(ValueError, match="duplicates"):
        reader.read(source, layout=layout, region_ids=["body-r1", "body-r1"])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")
