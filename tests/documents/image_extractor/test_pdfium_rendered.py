"""PdfiumImageExtractor in rendered mode (the default): crops of pictures as they appear
on the page, standalone or by region."""

import sys
from io import BytesIO

import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PdfiumImageExtractor,
)
from raggae.documents.layout_analysis import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import INLINE, draw, make_layout

RED = (200, 30, 30)  # the "rgb" test picture
BLUE = (0, 0, 255)  # the bars drawn with rectangles


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def sizes(result):
    return [(p.page_index, p.region_id, p.width, p.height) for p in result.images]


def centre(picture):
    with Image.open(BytesIO(picture.data)) as image:
        return image.getpixel((image.width // 2, image.height // 2))


def extract(data, dpi=72, **options):
    """At 72 DPI one point is one pixel, so crop sizes read as points."""
    return PdfiumImageExtractor(mode="rendered", dpi=dpi).extract(data, **options)


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(tmp_path, pdf, source_kind):
    pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 10, 10, 40, 30))
    data = pdf.save()
    path = tmp_path / "manual.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = PdfiumImageExtractor()  # rendered mode at 300 DPI by default
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pdfium"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert (picture.media_type, picture.method) == ("image/png", "rendered")
    # 40 × 30 pt at 300 DPI is 166.7 × 125 pixels; a crop keeps every pixel the
    # picture touches (41.7 to 208.3 across, 1612.5 to 1737.5 down).
    assert (picture.width, picture.height) == (168, 126)
    with Image.open(BytesIO(picture.data)) as image:
        assert image.mode == "RGB" and image.size == (168, 126)
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("dpi", [None, "300", True, 0, -72, float("inf"), float("nan")])
def test_dpi_is_validated(dpi):
    with pytest.raises((TypeError, ValueError)):
        PdfiumImageExtractor(dpi=dpi)


def test_dpi_sets_the_crop_resolution(pdf):
    pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 10, 10, 40, 30))
    data = pdf.save()
    assert sizes(extract(data, dpi=72)) == [(0, None, 40, 30)]
    assert sizes(extract(data, dpi=144)) == [(0, None, 80, 60)]


# Standalone: each drawn picture ------------------------------------------


def test_each_drawn_picture_is_cropped_where_it_is_drawn(pdf):
    logo, unused = pdf.picture("rgb"), pdf.picture("jpeg")
    pdf.page(
        {"/Logo": logo, "/Unused": unused},
        draw("Logo", 10, 300, 40, 40) + draw("Logo", 100, 100, 80, 60),
    )
    pdf.page({"/Logo": logo}, draw("Logo", 20, 20, 30, 30))
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.findings
    assert sizes(result) == [(0, None, 40, 40), (0, None, 80, 60), (1, None, 30, 30)]
    assert all(centre(picture) == RED for picture in result.images)


def test_inline_images_and_pictures_inside_nested_forms_are_cropped(pdf):
    inner = pdf.form({"/Deep": pdf.picture("rgb")}, draw("Deep", 0, 0, 10, 10))
    inner.Matrix = [3, 0, 0, 3, 0, 0]
    outer = pdf.form({"/Inner": inner}, b"/Inner Do")
    # Clip boxes large enough to show the whole 30 × 30 picture.
    inner.BBox = outer.BBox = [0, 0, 100, 100]
    pdf.page({"/Outer": outer}, b"q 1 0 0 1 100 200 cm /Outer Do Q\n" + INLINE)
    result = extract(pdf.save())
    assert sizes(result) == [(0, None, 30, 30), (0, None, 40, 40)]
    assert centre(result.images[0]) == RED


def test_a_page_turned_a_quarter_turn(pdf):
    # A 100 × 50 picture at PDF (50, 300) is displayed 50 wide and 100 tall.
    pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 50, 300, 100, 50), rotate=90)
    data = pdf.save()
    [picture] = extract(data).images
    assert (picture.width, picture.height) == (50, 100) and centre(picture) == RED
    layout = make_layout(("shown", 1, "image", (300, 50, 350, 150)), size=(400, 300))
    [region] = extract(data, layout=layout).images
    assert (region.width, region.height) == (50, 100) and centre(region) == RED


def test_a_cropped_page_keeps_only_pictures_in_the_visible_area(pdf):
    pdf.page(
        {"/Im": pdf.picture("rgb")},
        draw("Im", 10, 250, 40, 40) + draw("Im", 10, 10, 40, 40),
        crop=[0, 200, 300, 400],
    )
    [picture] = extract(pdf.save()).images
    assert (picture.width, picture.height) == (40, 40) and centre(picture) == RED


def test_a_chart_drawn_with_lines_is_found_only_through_a_region(pdf):
    pdf.page(content=b"0 0 1 rg 30 50 40 120 re f\n")  # displayed (30, 230, 70, 350)
    data = pdf.save()
    result = extract(data)
    assert result.outcome == "success" and not result.images
    assert codes(result) == [("empty", None, None)]
    layout = make_layout(("chart", 1, "image", (20, 220, 80, 360)))
    [chart] = extract(data, layout=layout).images
    assert chart.region_id == "chart" and centre(chart) == BLUE


# Layout regions ----------------------------------------------------------


@pytest.fixture
def regions_pdf(pdf):
    pdf.page({"/Photo": pdf.picture("rgb")}, draw("Photo", 20, 300, 100, 80))
    return pdf.save()


PHOTO = ("photo", 1, "image", (20, 20, 120, 100))
WIDE = ("wide", 1, "image", (0, 0, 150, 150))
HEADING = ("heading", 1, "text", (20, 380, 280, 395))


def test_only_image_regions_by_default_and_overlaps_allowed(regions_pdf):
    result = extract(regions_pdf, layout=make_layout(PHOTO, WIDE, HEADING))
    assert result.outcome == "success" and not result.findings
    assert sizes(result) == [(0, "photo", 100, 80), (0, "wide", 150, 150)]
    assert centre(result.images[0]) == RED


def test_explicit_regions_are_cropped_whatever_their_kind(regions_pdf):
    layout = make_layout(PHOTO, WIDE, HEADING)
    result = extract(regions_pdf, layout=layout, region_ids=["heading"])
    assert sizes(result) == [(0, "heading", 260, 15)]


def test_regions_that_do_not_fit_the_source(regions_pdf):
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            *make_layout(pages=2).surfaces,
            LayoutSurface(
                id_="frame-1",
                kind="image",
                location=ImageFrameLocation(frame_number=1),
                width=100,
                height=100,
            ),
        ),
        regions=(
            LayoutRegion(id="photo", surface_id="page-1", kind="image", bbox=PHOTO[3]),
            LayoutRegion(id="gone", surface_id="page-2", kind="image", bbox=(0, 0, 9, 9)),
            LayoutRegion(id="frame", surface_id="frame-1", kind="image", bbox=(0, 0, 9, 9)),
        ),
    )
    result = extract(regions_pdf, layout=layout)
    assert result.outcome == "partial"
    assert sizes(result) == [(0, "photo", 100, 80)]
    assert codes(result) == [
        ("page_missing", 1, "gone"),
        ("selection_unsupported", None, "frame"),
    ]
    smaller = extract(regions_pdf, layout=make_layout(PHOTO, size=(200, 400)))
    assert smaller.outcome == "failed"
    assert codes(smaller) == [("layout_mismatch", 0, "photo")]


def test_nothing_selected_without_opening_the_file(tmp_path):
    missing = tmp_path / "missing.pdf"
    explicit = extract(missing, layout=make_layout(PHOTO), region_ids=[])
    no_images = extract(missing, layout=make_layout(HEADING))
    for result in (explicit, no_images):
        assert result.outcome == "success"
        assert codes(result) == [("nothing_selected", None, None)]
    assert explicit.findings[0].message != no_images.findings[0].message


# Size, failures, and sources ---------------------------------------------


def test_page_above_pillows_ceiling_is_not_rendered(pdf, monkeypatch):
    pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 10, 10, 40, 30))
    pdf.page()
    data = pdf.save()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # a 300 × 400 page is 120 000
    result = extract(data)
    assert result.outcome == "partial" and not result.images
    # The empty second page needs no rendering, so it gets no finding.
    assert codes(result) == [("image_too_large", 0, None)]
    by_region = extract(data, layout=make_layout(PHOTO))
    assert by_region.outcome == "failed"
    assert codes(by_region) == [("image_too_large", 0, "photo")]


def test_page_that_cannot_be_rendered_keeps_other_pages(pdf, monkeypatch):
    from raggae.documents.pages import PdfPages

    real = PdfPages.render

    def flaky(self, index):
        if index == 1:
            raise RuntimeError("page could not be rendered")
        return real(self, index)

    monkeypatch.setattr(PdfPages, "render", flaky)
    for _ in range(3):
        pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 10, 10, 40, 30))
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [0, 2]
    assert codes(result) == [("page_unreadable", 1, None)]


def test_passwords(pdf):
    pdf.page({"/Im": pdf.picture("rgb")}, draw("Im", 10, 10, 40, 30))
    locked = pdf.save(password="right-password")
    assert codes(extract(locked)) == [("password_required", None, None)]
    opened = extract(Artifact(data=locked, password="right-password"))
    assert sizes(opened) == [(0, None, 40, 30)]
    assert "right-password" not in opened.model_dump_json()


def test_unusable_sources(tmp_path, pdf):
    assert extract(b"plain text, not a PDF").outcome == "not_applicable"
    assert codes(extract(tmp_path / "missing.pdf")) == [
        ("source_unreadable", None, None)
    ]
    assert codes(extract(pdf.save())) == [("no_pages", None, None)]


def test_missing_pypdfium2_names_the_extra(pdf, monkeypatch):
    pdf.page()
    data = pdf.save()
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    with pytest.raises(ImportError, match=r"raggae\[image-pdf\]"):
        extract(data)
