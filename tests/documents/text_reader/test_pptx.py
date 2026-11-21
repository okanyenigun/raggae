"""Slide shape text by slide, standalone or from PowerPoint layout regions."""

import builtins
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_CONNECTOR
from pptx.util import Inches

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.layout_analysis.models import PptxShapeLocation, PptxSlideLocation
from raggae.documents.layout_analysis.region_discovery.pptx import PptxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import PptxTextReader, TextReader, TextReadResult

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
BLANK, TITLE_ONLY, TITLE_AND_CONTENT = 6, 5, 1


def save(presentation):
    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def textbox(slide, text, top=1):
    box = slide.shapes.add_textbox(Inches(1), Inches(top), Inches(4), Inches(1))
    box.text_frame.text = text
    return box


def deck(*slide_texts):
    """One blank slide per item; each item is a list of text-box texts."""
    presentation = Presentation()
    for texts in slide_texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
        for number, text in enumerate(texts):
            textbox(slide, text, top=1 + number)
    return presentation


def png():
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def picture(slide, alt):
    """A picture with this alt text, or none at all when alt is None.

    python-pptx stores the image file name as the description by default.
    """
    shape = slide.shapes.add_picture(png(), Inches(5), Inches(1))
    properties = shape._element.xpath("./p:nvPicPr/p:cNvPr")[0]
    if alt is None:
        properties.attrib.pop("descr", None)
    else:
        properties.set("descr", alt)
    return shape


def chart(slide):
    data = CategoryChartData()
    data.categories = ["a"]
    data.add_series("Revenue", (1,))
    return slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(3), Inches(2), Inches(2), data
    )


def read(presentation, **kwargs):
    data = presentation if isinstance(presentation, bytes) else save(presentation)
    return PptxTextReader(**{k: kwargs.pop(k) for k in ["notes"] if k in kwargs}).read(
        data, **kwargs
    )


def texts(result):
    return [block.text for block in result.blocks]


def pages(result):
    return [(block.page_index, block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def by_region(result):
    return {block.region_id: block.text for block in result.blocks}


# Standalone reading -------------------------------------------------------


def test_pptx_family_exports_the_same_worker():
    from raggae.documents.text_reader import pptx as pptx_readers

    assert pptx_readers.PptxTextReader is PptxTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind, keyword):
    data = save(deck(["Garantie"], ["Café İstanbul"]))
    path = tmp_path / "deck.pptx"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = PptxTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pptx"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert pages(result) == [(0, "Garantie"), (1, "Café İstanbul")]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_one_block_per_shape_in_storage_order_with_slide_page_index():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[TITLE_AND_CONTENT])
    slide.shapes.title.text = "Pricing"
    body = slide.placeholders[1].text_frame
    body.text = "Monthly plan"
    body.add_paragraph().text = "Annual plan"
    textbox(slide, "Bottom note placed first visually", top=0)
    second = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
    textbox(second, "Second slide")
    assert pages(read(presentation)) == [
        (0, "Pricing"),
        (0, "Monthly plan\nAnnual plan"),
        (0, "Bottom note placed first visually"),
        (1, "Second slide"),
    ]


def test_line_breaks_fields_and_exact_paragraph_text():
    presentation = deck(["placeholder"])
    frame = presentation.slides[0].shapes[0].text_frame
    paragraph = frame.paragraphs[0]._p
    for child in list(paragraph):
        if child.tag == f"{{{A}}}r":
            paragraph.remove(child)
    for markup in (
        f'<a:r xmlns:a="{A}"><a:t>  Slide </a:t></a:r>',
        f'<a:fld xmlns:a="{A}" id="{{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}}" type="slidenum"><a:t>3</a:t></a:fld>',
        f'<a:br xmlns:a="{A}"/>',
        f'<a:r xmlns:a="{A}"><a:t>of\tten</a:t></a:r>',
    ):
        paragraph.append(etree.fromstring(markup))
    frame.add_paragraph().text = ""
    frame.add_paragraph().text = "After blank"
    frame.add_paragraph().text = ""
    assert texts(read(presentation)) == ["  Slide 3\nof\tten\n\nAfter blank"]


def test_empty_placeholders_and_whitespace_shapes_give_no_block():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[TITLE_AND_CONTENT])
    slide.shapes.title.text = "   "
    textbox(slide, "Only this")
    assert texts(read(presentation)) == ["Only this"]


def test_bullets_and_numbering_are_not_added():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[TITLE_AND_CONTENT])
    frame = slide.placeholders[1].text_frame
    frame.text = "First"
    frame.add_paragraph().text = "Second"
    frame.paragraphs[1].level = 1
    assert texts(read(presentation)) == ["First\nSecond"]


def test_tables_put_rows_on_lines_and_merged_cells_once():
    presentation = deck([])
    slide = presentation.slides[0]
    table = slide.shapes.add_table(
        4, 3, Inches(1), Inches(1), Inches(6), Inches(2)
    ).table
    for column, label in enumerate(("Part", "Years", "Note")):
        table.cell(0, column).text = label
    table.cell(1, 0).text, table.cell(1, 1).text = "Motor", "2"
    table.cell(2, 0).merge(table.cell(2, 2))
    table.cell(2, 0).text = "All parts"
    frame = table.cell(3, 0).text_frame
    frame.text = "First"
    frame.add_paragraph().text = "second  line"
    assert texts(read(presentation)) == [
        "Part | Years | Note\nMotor | 2 | \nAll parts\nFirst second line |  | "
    ]


def test_vertically_merged_cells_and_empty_rows():
    presentation = deck([])
    table = (
        presentation.slides[0]
        .shapes.add_table(3, 2, Inches(1), Inches(1), Inches(4), Inches(2))
        .table
    )
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 0).text = "Tall"
    table.cell(0, 1).text, table.cell(1, 1).text = "a", "b"
    assert texts(read(presentation)) == ["Tall | a\n | b"]


def test_picture_alt_text_is_read():
    presentation = deck([])
    slide = presentation.slides[0]
    picture(slide, "motor\n photo")
    picture(slide, None)
    assert texts(read(presentation)) == ["motor photo"]


def test_a_stored_file_name_description_is_read_as_is():
    presentation = deck([])
    presentation.slides[0].shapes.add_picture(png(), Inches(5), Inches(1))
    assert texts(read(presentation)) == ["image.png"]


def test_group_members_are_read_individually_including_nested_groups():
    presentation = deck([])
    slide = presentation.slides[0]
    outer = slide.shapes.add_group_shape()
    outer.shapes.add_textbox(
        Inches(1), Inches(1), Inches(2), Inches(1)
    ).text_frame.text = "A"
    inner = outer.shapes.add_group_shape()
    inner.shapes.add_textbox(
        Inches(1), Inches(2), Inches(2), Inches(1)
    ).text_frame.text = "B"
    textbox(slide, "C", top=4)
    assert texts(read(presentation)) == ["A", "B", "C"]


def test_charts_are_skipped_with_a_finding_and_connectors_silently():
    presentation = deck(["Before"], ["Revenue"])
    chart(presentation.slides[1])
    presentation.slides[0].shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(1), Inches(1), Inches(2), Inches(2)
    )
    result = read(presentation)
    assert result.outcome == "success"
    assert texts(result) == ["Before", "Revenue"]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("unsupported_content", 1)
    ]


def test_speaker_notes_are_reported_by_default_and_read_on_request():
    presentation = deck(["Slide one"], ["Slide two"])
    presentation.slides[0].notes_slide.notes_text_frame.text = "Mention the discount"
    presentation.slides[1].notes_slide.notes_text_frame.text = "   "
    default = read(presentation)
    assert pages(default) == [(0, "Slide one"), (1, "Slide two")]
    assert [(f.code, f.page_index) for f in default.findings] == [("notes_unread", 0)]
    assert "discount" not in default.model_dump_json()
    with_notes = read(presentation, notes=True)
    assert with_notes.outcome == "success" and not with_notes.findings
    assert pages(with_notes) == [
        (0, "Slide one"),
        (0, "Mention the discount"),
        (1, "Slide two"),
    ]
    assert all(block.region_id is None for block in with_notes.blocks)


@pytest.mark.parametrize("notes", [None, 1, "yes"])
def test_notes_option_must_be_a_bool(notes):
    with pytest.raises(TypeError, match="notes must be a bool"):
        PptxTextReader(notes=notes)


def test_hidden_slides_are_read_with_a_finding():
    presentation = deck(["Shown"], ["Backup"])
    presentation.slides[1]._element.set("show", "0")
    result = read(presentation)
    assert result.outcome == "success"
    assert pages(result) == [(0, "Shown"), (1, "Backup")]
    assert [(f.code, f.page_index) for f in result.findings] == [("hidden_slide", 1)]


def master_text_box(presentation, text):
    presentation.slide_master.shapes._spTree.append(
        etree.fromstring(
            f'<p:sp xmlns:p="{P}" xmlns:a="{A}"><p:nvSpPr><p:cNvPr id="99" name="t"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>'
            f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
        )
    )


def test_layout_and_master_text_is_reported_once_not_read():
    presentation = deck(["One"], ["Two"])
    assert not read(presentation).findings  # Default masters hold only placeholders.
    master_text_box(presentation, "Confidential")
    result = read(presentation)
    assert texts(result) == ["One", "Two"]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("inherited_text_unread", None)
    ]
    assert "Confidential" not in result.model_dump_json()


@pytest.mark.parametrize("presentation", [Presentation(), deck([], [""])])
def test_no_readable_text_succeeds_with_an_explanation(presentation):
    result = read(presentation)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


def test_repeated_wording_is_not_deduplicated():
    assert texts(read(deck(["Same", "Same"], ["Same"]))) == ["Same"] * 3


def test_too_deep_structure_is_reported(monkeypatch):
    import raggae.documents.text_reader.pptx.slides as module

    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(module.PptxTextReader, "_shapes_texts", staticmethod(too_deep))
    result = read(deck(["x"]))
    assert result.outcome == "failed"
    assert codes(result) == ["structure_unsupported"]


def reorder_first_to_last(data):
    presentation = Presentation(BytesIO(data))
    ids = presentation.element.sldIdLst
    ids.append(ids[0])
    return save(presentation)


def test_slide_order_comes_from_the_presentation_not_part_names():
    data = reorder_first_to_last(save(deck(["Was first"], ["Was second"])))
    names = ZipFile(BytesIO(data)).namelist()
    assert "ppt/slides/slide1.xml" in names and "ppt/slides/slide2.xml" in names
    result = PptxTextReader().read(data)
    assert pages(result) == [(0, "Was second"), (1, "Was first")]


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
        zip_bytes({"ppt/presentation.xml": "<x/>"}),
        zip_bytes({"[Content_Types].xml": "<Types/>"}),
    ],
)
def test_non_pptx_sources_fail_safely(data):
    result = PptxTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_word_document_is_not_read_as_a_presentation():
    from docx import Document

    output = BytesIO()
    Document().save(output)
    result = PptxTextReader().read(output.getvalue())
    assert result.outcome == "failed"
    assert codes(result) == ["decode_failed"]


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = PptxTextReader()
    result = reader.read(tmp_path / "private-name.pptx")
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
    data = save(deck(["x"]))
    real_import = builtins.__import__

    def without_pptx(name, *args, **kwargs):
        if name == "pptx" or name.startswith("pptx."):
            raise ImportError("no python-pptx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pptx)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        PptxTextReader().read(data)


def test_streams_are_not_silently_added_to_source_contract():
    with pytest.raises(TypeError, match="source must"):
        PptxTextReader().read(BytesIO(save(deck(["x"]))))


# Layout-guided reading -----------------------------------------------------


@pytest.fixture
def report():
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[TITLE_ONLY])
    first.shapes.title.text = "Coverage"
    table = first.shapes.add_table(
        2, 2, Inches(1), Inches(2), Inches(4), Inches(1)
    ).table
    table.cell(0, 0).text, table.cell(0, 1).text = "Part", "Years"
    table.cell(1, 0).text, table.cell(1, 1).text = "Motor", "2"
    picture(first, "motor photo")
    group = first.shapes.add_group_shape()
    group.shapes.add_textbox(
        Inches(1), Inches(4), Inches(2), Inches(1)
    ).text_frame.text = "Grouped note"
    chart(first)
    second = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
    textbox(second, "Backup")
    second._element.set("show", "0")
    second.notes_slide.notes_text_frame.text = "Private notes"
    data = save(presentation)
    return data, PptxLayoutAnalyzer().analyze(data)


def region(layout, kind, nth=0):
    return [r for r in layout.regions if r.kind == kind][nth]


def test_layout_regions_from_the_slide_analyzer_are_read(report):
    data, layout = report
    title, table = region(layout, "heading"), region(layout, "table")
    result = PptxTextReader().read(data, layout=layout, region_ids=[table.id, title.id])
    assert result.outcome == "success" and not result.findings
    assert [(b.region_id, b.page_index, b.text) for b in result.blocks] == [
        (table.id, 0, "Part | Years\nMotor | 2"),
        (title.id, 0, "Coverage"),
    ]


def test_selecting_all_regions(report):
    data, layout = report
    result = PptxTextReader(notes=True).read(data, layout=layout)
    assert result.outcome == "partial"  # The chart region cannot be read.
    assert sorted(texts(result)) == sorted(
        ["Coverage", "Part | Years\nMotor | 2", "motor photo", "Grouped note", "Backup"]
    )
    assert "Private notes" not in result.model_dump_json()
    found = [(f.code, f.region_id, f.page_index) for f in result.findings]
    assert (
        "selection_unsupported",
        region(layout, "unknown").id,
        0,
    ) in found
    assert ("hidden_slide", None, 1) in found
    assert "notes_unread" not in codes(result)


def slide_layout(part, *shapes, slide_number=1):
    """A hand-made map; each shape is (shape_path, shape_id)."""
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="slide-1",
                kind="slide",
                location=PptxSlideLocation(slide_number=slide_number, part=part),
                width=720,
                height=540,
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=f"r{number}",
                surface_id="slide-1",
                kind="text",
                location=PptxShapeLocation(
                    part=part, shape_id=shape_id, shape_path=path
                ),
            )
            for number, (path, shape_id) in enumerate(shapes, start=1)
        ),
    )


def grouped_slide():
    presentation = deck([])
    slide = presentation.slides[0]
    group = slide.shapes.add_group_shape()
    first = group.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    first.text_frame.text = "Inside one"
    second = group.shapes.add_textbox(Inches(1), Inches(2), Inches(2), Inches(1))
    second.text_frame.text = "Inside two"
    return save(presentation), group.shape_id, first.shape_id, second.shape_id


def test_group_region_reads_members_and_inner_selection_owns_its_text():
    data, group_id, first_id, _ = grouped_slide()
    part = "ppt/slides/slide1.xml"
    alone = PptxTextReader().read(data, layout=slide_layout(part, ((0,), group_id)))
    assert by_region(alone) == {"r1": "Inside one\nInside two"}
    both = PptxTextReader().read(
        data, layout=slide_layout(part, ((0,), group_id), ((0, 0), first_id))
    )
    assert by_region(both) == {"r1": "Inside two", "r2": "Inside one"}


@pytest.mark.parametrize(
    "path,shape_id",
    [((9,), 2), ((0,), 999), ((0, 0), None)],
)
def test_unresolved_shapes_are_reported(path, shape_id):
    data = save(deck(["Kept", "Other"]))
    presentation = Presentation(BytesIO(data))
    ids = [shape.shape_id for shape in presentation.slides[0].shapes]
    part = "ppt/slides/slide1.xml"
    layout = slide_layout(part, ((0,), ids[0]), (path, shape_id or ids[1]))
    result = PptxTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r1": "Kept"}
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("region_unresolved", "r2")
    ]


def test_layout_for_an_unknown_slide_part_is_not_resolved():
    data = save(deck(["Kept"]))
    layout = slide_layout("ppt/slides/slide9.xml", ((0,), 2))
    result = PptxTextReader().read(data, layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("region_unresolved", "r1", None)
    ]


def test_regions_follow_original_part_names_after_reordering():
    data = save(deck(["Was first"], ["Was second"]))
    layout = PptxLayoutAnalyzer().analyze(reorder_first_to_last(data))
    reordered = reorder_first_to_last(data)
    result = PptxTextReader().read(reordered, layout=layout)
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (0, "Was second"),
        (1, "Was first"),
    ]
    assert result.outcome == "success"


def test_two_regions_locating_the_same_shape_are_both_rejected():
    data = save(deck(["Same", "Other"]))
    ids = [s.shape_id for s in Presentation(BytesIO(data)).slides[0].shapes]
    part = "ppt/slides/slide1.xml"
    layout = slide_layout(part, ((0,), ids[0]), ((0,), ids[0]), ((1,), ids[1]))
    result = PptxTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert by_region(result) == {"r3": "Other"}
    assert codes(result) == ["selection_overlap"] * 2


def test_connector_region_is_an_empty_read():
    presentation = deck([])
    connector = presentation.slides[0].shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(1), Inches(1), Inches(2), Inches(2)
    )
    layout = slide_layout("ppt/slides/slide1.xml", ((0,), connector.shape_id))
    result = PptxTextReader().read(save(presentation), layout=layout)
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("empty", "r1", 0)
    ]


def test_non_slide_locations_are_rejected():
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
    result = PptxTextReader().read(save(deck(["Whole"])), layout=layout)
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
    result = PptxTextReader().read(
        tmp_path / "missing.pptx", layout=layout, region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_layout_argument_validation(tmp_path, report):
    _, layout = report
    reader = PptxTextReader()
    source = tmp_path / "does-not-exist"
    first = layout.regions[0].id
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=layout, region_ids=["unknown"])
    with pytest.raises(ValueError, match="duplicates"):
        reader.read(source, layout=layout, region_ids=[first, first])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")
