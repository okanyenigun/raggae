"""Pictures on PowerPoint slides, standalone or by layout region."""

import copy
import sys
from io import BytesIO

import pytest
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Inches

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PptxImageExtractor,
)
from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    PptxShapeLocation,
)
from raggae.documents.layout_analysis.region_discovery.pptx import PptxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import make_layout, picture_file, rezip

BLANK = 6


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [(p.page_index, p.region_id, p.media_type, p.method) for p in result.images]


def save(presentation):
    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def extract(data, **options):
    return PptxImageExtractor().extract(data, **options)


@pytest.fixture
def deck():
    """Slide 1: a PNG picture and a group holding a JPEG; slide 2: a title and a PNG."""
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
    first.shapes.add_picture(picture_file("red"), Inches(1), Inches(1))
    group = first.shapes.add_group_shape()
    group.shapes.add_picture(picture_file("blue", "JPEG", (20, 10)), Inches(4), Inches(1))
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Results"
    second.shapes.add_picture(picture_file("green"), Inches(1), Inches(2))
    return save(presentation)


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import pptx

    assert pptx.PptxImageExtractor is PptxImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, deck, source_kind):
    path = tmp_path / "deck.pptx"
    path.write_bytes(deck)
    source = {
        "path": path,
        "string": str(path),
        "bytes": deck,
        "artifact": Artifact(data=deck, password="private-password"),
    }[source_kind]
    extractor = PptxImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pptx"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, None, "image/png", "original"),
        (0, None, "image/jpeg", "original"),
        (1, None, "image/png", "original"),
    ]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == deck and list(tmp_path.iterdir()) == [path]


# Which pictures ------------------------------------------------------------


def test_page_index_follows_the_slide_order_not_the_part_names(deck):
    presentation = Presentation(BytesIO(deck))
    slide_ids = presentation.element.sldIdLst
    first = slide_ids[0]
    slide_ids.remove(first)
    slide_ids.append(first)  # slide1.xml now comes second
    result = extract(save(presentation))
    assert [(p.page_index, p.media_type) for p in result.images] == [
        (0, "image/png"),
        (1, "image/png"),
        (1, "image/jpeg"),
    ]


def test_picture_placeholders_and_stored_descriptions():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[8])  # picture + caption
    placeholder = next(s for s in slide.placeholders if s.placeholder_format.idx == 1)
    picture = placeholder.insert_picture(picture_file("red"))
    picture.element.nvPicPr.cNvPr.set("descr", "Team photo")
    [image] = extract(save(presentation)).images
    assert (image.media_type, image.alt_text) == ("image/png", "Team photo")


def test_cropped_hidden_and_chart_slides(deck):
    presentation = Presentation(BytesIO(deck))
    first, second = presentation.slides
    first.shapes[0].crop_left = 0.25
    second.element.set("show", "0")
    data = CategoryChartData()
    data.categories = ["a", "b"]
    data.add_series("sales", (1, 2))
    second.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(3), Inches(2), data
    )
    result = extract(save(presentation))
    assert result.outcome == "success" and len(result.images) == 3
    assert codes(result) == [
        ("crop_not_applied", 0, None),
        ("hidden_slide", 1, None),
        ("unsupported_content", 1, None),
    ]


def test_picture_fills_and_pictures_on_layouts_are_reported():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
    box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(2))
    _, relation = slide.part.get_or_add_image_part(picture_file())
    fill = box.element.spPr.makeelement(qn("a:blipFill"), {})
    fill.append(fill.makeelement(qn("a:blip"), {qn("r:embed"): relation}))
    box.element.spPr.insert(1, fill)
    layout = presentation.slide_layouts[BLANK]
    _, layout_relation = layout.part.get_or_add_image_part(picture_file("blue"))
    picture = copy.deepcopy(
        slide.shapes.add_picture(picture_file(), Inches(4), Inches(1)).element
    )
    next(picture.iter(qn("a:blip"))).set(qn("r:embed"), layout_relation)
    layout.shapes._spTree.append(picture)
    result = extract(save(presentation))
    assert [p.media_type for p in result.images] == ["image/png"]
    assert codes(result) == [
        ("unsupported_content", 0, None),
        ("other_parts_unread", None, None),
    ]


def test_a_presentation_without_pictures_is_empty():
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[5]).shapes.title.text = "Hi"
    result = extract(save(presentation))
    assert result.outcome == "success" and codes(result) == [("empty", None, None)]


# Formats and links ------------------------------------------------------------


def test_other_raster_formats_become_new_files_and_vector_ones_findings():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[BLANK])
    slide.shapes.add_picture(picture_file("red", "BMP"), Inches(1), Inches(1))
    slide.shapes.add_picture(picture_file("blue"), Inches(3), Inches(1))
    data = save(presentation)
    bmp, png = extract(data).images
    assert (bmp.media_type, bmp.method) == ("image/png", "reencoded")
    with Image.open(BytesIO(bmp.data)) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
    declared = rezip(
        data,
        replace={
            "[Content_Types].xml": lambda xml: xml.replace(
                b'ContentType="image/png"', b'ContentType="image/x-emf"'
            )
        },
    )
    result = extract(declared)
    assert result.outcome == "partial" and len(result.images) == 1
    assert codes(result) == [("unsupported_image_type", 0, None)]


def test_a_linked_picture_is_not_fetched(deck):
    presentation = Presentation(BytesIO(deck))
    slide = presentation.slides[0]
    blip = next(slide.shapes[0].element.iter(qn("a:blip")))
    relation = slide.part.relate_to(
        "https://example.com/logo.png",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        is_external=True,
    )
    del blip.attrib[qn("r:embed")]
    blip.set(qn("r:link"), relation)
    result = extract(save(presentation))
    assert result.outcome == "success" and len(result.images) == 2
    assert codes(result) == [("reference_unresolved", 0, None)]
    assert "example.com" not in result.model_dump_json()


def test_unusable_sources(tmp_path):
    from docx import Document

    document = BytesIO()
    Document().save(document)
    for data in (b"", b"not a zip file", document.getvalue()):
        result = extract(data)
        assert result.outcome == "failed" and codes(result)[0][0] == "decode_failed"
    assert codes(extract(tmp_path / "missing.pptx")) == [("source_unreadable", None, None)]


def test_missing_python_pptx_names_the_extra(deck, monkeypatch):
    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        extract(deck)


# Layout regions ----------------------------------------------------------------


@pytest.fixture
def layout(deck):
    return PptxLayoutAnalyzer().analyze(deck)


def ids(layout, kind):
    return [region.id for region in layout.regions if region.kind == kind]


def test_layout_links_each_picture_to_its_image_region(deck, layout):
    images = ids(layout, "image")
    assert len(images) == 3
    result = extract(deck, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert [(p.page_index, p.region_id) for p in result.images] == [
        (0, images[0]),
        (0, images[1]),
        (1, images[2]),
    ]


def test_explicit_regions_of_other_kinds(deck, layout):
    heading = ids(layout, "heading")[0]
    result = extract(deck, layout=layout, region_ids=[heading])
    assert result.outcome == "success" and codes(result) == [("empty", 1, heading)]


def test_chart_regions_hidden_slides_and_unresolved_shapes(deck):
    presentation = Presentation(BytesIO(deck))
    second = presentation.slides[1]
    second.element.set("show", "0")
    data = CategoryChartData()
    data.categories = ["a"]
    data.add_series("sales", (1,))
    second.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(3), Inches(2), data
    )
    changed = save(presentation)
    layout = PptxLayoutAnalyzer().analyze(changed)
    chart = next(
        r.id for r in layout.regions if r.kind == "unknown" and r.surface_id == "slide-2"
    )
    picture = ids(layout, "image")[2]
    surface = next(s for s in layout.surfaces if s.id_ == "slide-2")
    wrong_id = LayoutRegion(
        id="wrong-id", surface_id="slide-2", kind="image",
        location=PptxShapeLocation(part=surface.location.part, shape_id=999, shape_path=(1,)),
    )
    custom = LayoutAnalysisResult(
        outcome="success", surfaces=layout.surfaces, regions=(*layout.regions, wrong_id)
    )
    result = extract(changed, layout=custom, region_ids=[chart, picture, "wrong-id"])
    assert result.outcome == "partial"
    assert [(p.page_index, p.region_id) for p in result.images] == [(1, picture)]
    assert codes(result) == [
        ("selection_unsupported", None, chart),
        ("region_unresolved", 1, "wrong-id"),
        ("hidden_slide", 1, None),
    ]


def test_regions_locating_the_same_shape_are_refused(deck, layout):
    first = next(r for r in layout.regions if r.kind == "image")
    twin = first.model_copy(update={"id": "twin"})
    custom = LayoutAnalysisResult(
        outcome="success", surfaces=layout.surfaces, regions=(*layout.regions, twin)
    )
    result = extract(deck, layout=custom, region_ids=[first.id, "twin"])
    assert result.outcome == "failed" and not result.images
    assert codes(result) == [
        ("selection_overlap", 0, first.id),
        ("selection_overlap", 0, "twin"),
    ]


def test_non_slide_locations_and_empty_selections(tmp_path, deck):
    pdf_layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    assert codes(extract(deck, layout=pdf_layout)) == [
        ("selection_unsupported", None, "photo")
    ]
    missing = tmp_path / "missing.pptx"
    assert codes(extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None, None)
    ]
