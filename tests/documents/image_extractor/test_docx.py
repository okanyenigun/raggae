"""Pictures in a Word document's main body, standalone or by layout region."""

import copy
import sys
from io import BytesIO

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches
from PIL import Image

from raggae.documents.image_extractor import (
    DocxImageExtractor,
    ImageExtractor,
    ImageExtractResult,
)
from raggae.documents.layout_analysis import (
    DocxBodyLocation,
    DocxElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.layout_analysis.region_discovery.docx import DocxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import make_layout, picture_file, rezip

R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
VML = "{urn:schemas-microsoft-com:vml}"


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [
        (p.region_id, p.media_type, p.method, p.width, p.height) for p in result.images
    ]


def save(document):
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def extract(data, **options):
    return DocxImageExtractor().extract(data, **options)


def last_drawing(document):
    return list(document.element.body.iter(qn("w:drawing")))[-1]


@pytest.fixture
def report():
    """Body: a paragraph, a PNG picture, a table holding a JPEG picture, a paragraph."""
    document = Document()
    document.add_paragraph("Introduction")
    document.add_picture(picture_file("red"), width=Inches(1))
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0).paragraphs[0]
    cell.add_run().add_picture(picture_file("blue", "JPEG", (20, 10)), width=Inches(1))
    document.add_paragraph("Closing words")
    return save(document)


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import docx

    assert docx.DocxImageExtractor is DocxImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, report, source_kind):
    path = tmp_path / "report.docx"
    path.write_bytes(report)
    source = {
        "path": path,
        "string": str(path),
        "bytes": report,
        "artifact": Artifact(data=report, password="private-password"),
    }[source_kind]
    extractor = DocxImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_docx"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (None, "image/png", "original", 40, 30),
        (None, "image/jpeg", "original", 20, 10),
    ]
    assert all(picture.page_index == 0 for picture in result.images)
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == report and list(tmp_path.iterdir()) == [path]


# Which pictures ------------------------------------------------------------


def test_floating_pictures_and_repeated_pictures(report):
    document = Document(BytesIO(report))
    floating = last_drawing(document)
    inline = floating[0]
    inline.tag = qn("wp:anchor")  # the same picture, positioned instead of inline
    document.add_paragraph().add_run()._r.append(copy.deepcopy(floating))
    result = extract(save(document))
    assert [p.media_type for p in result.images] == ["image/png", "image/jpeg", "image/jpeg"]


def test_alt_text_is_the_stored_description(report):
    document = Document(BytesIO(report))
    next(last_drawing(document).iter(qn("wp:docPr"))).set("descr", "Blue logo")
    assert [p.alt_text for p in extract(save(document)).images] == [None, "Blue logo"]


def test_legacy_vml_picture_with_alt_text(report):
    document = Document(BytesIO(report))
    blip = next(last_drawing(document).iter(qn("a:blip")))
    relation = blip.get(qn("r:embed"))
    pict = document.element.makeelement(qn("w:pict"), {})
    shape = pict.makeelement(f"{VML}shape", {"alt": "Old logo"})
    shape.append(shape.makeelement(f"{VML}imagedata", {f"{R}id": relation}))
    pict.append(shape)
    document.add_paragraph().add_run()._r.append(pict)
    result = extract(save(document))
    assert result.images[-1].alt_text == "Old logo"
    assert result.images[-1].media_type == "image/jpeg"


def test_alternate_content_is_read_once(report):
    document = Document(BytesIO(report))
    drawing = last_drawing(document)
    run = drawing.getparent()
    alternate = run.makeelement(f"{MC}AlternateContent", {})
    choice = alternate.makeelement(f"{MC}Choice", {"Requires": "wps"})
    fallback = alternate.makeelement(f"{MC}Fallback", {})
    choice.append(copy.deepcopy(drawing))
    fallback.append(copy.deepcopy(drawing))
    alternate.extend([choice, fallback])
    run.replace(drawing, alternate)
    assert [p.media_type for p in extract(save(document)).images] == [
        "image/png",
        "image/jpeg",
    ]


def test_pictures_in_deleted_tracked_content_are_left_out(report):
    document = Document(BytesIO(report))
    drawing = last_drawing(document)
    run = drawing.getparent()
    deleted = run.makeelement(qn("w:del"), {qn("w:id"): "1", qn("w:author"): "A"})
    run.getparent().replace(run, deleted)
    deleted.append(run)
    assert [p.media_type for p in extract(save(document)).images] == ["image/png"]


def test_headers_charts_and_empty_bodies(report):
    document = Document(BytesIO(report))
    document.sections[0].header.paragraphs[0].add_run().add_picture(picture_file())
    shape = document.element.makeelement(qn("w:drawing"), {})  # a drawing with no picture
    document.add_paragraph().add_run()._r.append(shape)
    result = extract(save(document))
    assert result.outcome == "success" and len(result.images) == 2
    assert codes(result) == [
        ("other_parts_unread", None, None),
        ("unsupported_content", None, None),
    ]
    empty = Document()
    empty.add_paragraph("Only words.")
    assert codes(extract(save(empty))) == [("empty", None, None)]


# Formats -------------------------------------------------------------------


def test_other_raster_formats_become_new_files_and_vector_ones_findings():
    document = Document()
    document.add_picture(picture_file("red", "BMP"))
    document.add_picture(picture_file("blue"))
    data = save(document)
    bmp, png = extract(data).images
    assert (bmp.media_type, bmp.method) == ("image/png", "reencoded")
    with Image.open(BytesIO(bmp.data)) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
    # The same document with its PNG declared as EMF, a vector format.
    declared = rezip(
        data,
        replace={
            "[Content_Types].xml": lambda xml: xml.replace(
                b'Extension="png" ContentType="image/png"',
                b'Extension="png" ContentType="image/x-emf"',
            )
        },
    )
    result = extract(declared)
    assert result.outcome == "partial" and len(result.images) == 1
    assert codes(result) == [("unsupported_image_type", 0, None)]


def test_a_linked_picture_is_not_fetched(report):
    document = Document(BytesIO(report))
    blip = next(last_drawing(document).iter(qn("a:blip")))
    relation = document.part.relate_to(
        "https://example.com/logo.png",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        is_external=True,
    )
    del blip.attrib[qn("r:embed")]
    blip.set(qn("r:link"), relation)
    result = extract(save(document))
    assert result.outcome == "success"
    assert [p.media_type for p in result.images] == ["image/png"]
    assert codes(result) == [("reference_unresolved", 0, None)]
    assert "example.com" not in result.model_dump_json()


# Unusable sources ------------------------------------------------------------


def test_unusable_sources(tmp_path):
    from pptx import Presentation

    deck = BytesIO()
    Presentation().save(deck)
    for data in (b"", b"not a zip file", deck.getvalue()):
        result = extract(data)
        assert result.outcome == "failed"
        assert codes(result)[0][0] == "decode_failed"
    assert codes(extract(tmp_path / "missing.docx")) == [("source_unreadable", None, None)]


def test_missing_python_docx_names_the_extra(report, monkeypatch):
    monkeypatch.setitem(sys.modules, "docx", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        extract(report)


# Layout regions --------------------------------------------------------------


@pytest.fixture
def layout(report):
    return DocxLayoutAnalyzer().analyze(report)


def ids(layout, kind):
    return [region.id for region in layout.regions if region.kind == kind]


def test_layout_links_each_picture_to_its_image_region(report, layout):
    assert ids(layout, "image") == ["body-r2", "body-r4"]
    result = extract(report, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        ("body-r2", "image/png", "original", 40, 30),
        ("body-r4", "image/jpeg", "original", 20, 10),
    ]


def test_explicit_regions_give_the_pictures_inside_them(report, layout):
    table, text = ids(layout, "table")[0], ids(layout, "text")[0]
    result = extract(report, layout=layout, region_ids=[table, text])
    assert summary(result) == [(table, "image/jpeg", "original", 20, 10)]
    assert codes(result) == [("empty", 0, text)]


def test_the_innermost_selected_region_owns_a_picture(report, layout):
    table = ids(layout, "table")[0]
    result = extract(report, layout=layout, region_ids=[table, "body-r4"])
    assert [p.region_id for p in result.images] == ["body-r4"]
    assert not result.findings


def test_regions_that_cannot_be_resolved(report, layout):
    surface = layout.surfaces[0]
    regions = (
        LayoutRegion(
            id="same-a", surface_id=surface.id_, kind="image",
            location=DocxElementLocation(part="word/document.xml", child_indexes=(1,)),
        ),
        LayoutRegion(
            id="same-b", surface_id=surface.id_, kind="image",
            location=DocxElementLocation(part="word/document.xml", child_indexes=(1,)),
        ),
        LayoutRegion(
            id="far", surface_id=surface.id_, kind="image",
            location=DocxElementLocation(part="word/document.xml", child_indexes=(99,)),
        ),
        # A valid layout for a part this document does not have.
        LayoutRegion(
            id="other-part", surface_id="other", kind="image",
            location=DocxElementLocation(part="word/other.xml", child_indexes=(0,)),
        ),
    )
    other = LayoutSurface(
        id_="other", kind="flow", location=DocxBodyLocation(part="word/other.xml")
    )
    custom = LayoutAnalysisResult(
        outcome="success", surfaces=(surface, other), regions=regions
    )
    result = extract(report, layout=custom)
    assert result.outcome == "failed" and not result.images
    assert codes(result) == [
        ("region_unresolved", 0, "far"),
        ("region_unresolved", 0, "other-part"),
        ("selection_overlap", 0, "same-a"),
        ("selection_overlap", 0, "same-b"),
    ]


def test_non_word_locations_and_empty_selections(tmp_path, report):
    pdf_layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    result = extract(report, layout=pdf_layout)
    assert codes(result) == [("selection_unsupported", None, "photo")]
    missing = tmp_path / "missing.docx"
    assert codes(extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None, None)
    ]
