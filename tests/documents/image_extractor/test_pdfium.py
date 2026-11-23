"""PdfiumImageExtractor in stored mode: the stored picture per drawn occurrence,
standalone or by region; and the mode option itself."""

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


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [
        (p.page_index, p.region_id, p.media_type, p.method, p.width, p.height)
        for p in result.images
    ]


def extract(data, **options):
    return PdfiumImageExtractor(mode="stored").extract(data, **options)


def test_package_and_pdf_family_export_the_same_worker():
    from raggae.documents.image_extractor import pdf as pdf_extractors

    assert pdf_extractors.PdfiumImageExtractor is PdfiumImageExtractor


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(tmp_path, jpeg_pdf, source_kind):
    data, stored = jpeg_pdf
    path = tmp_path / "manual.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = PdfiumImageExtractor(mode="stored")
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pdfium"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert picture.data == stored
    assert summary(result) == [(0, None, "image/jpeg", "original", 4, 3)]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mode", [None, 1, "embedded", "Rendered"])
def test_mode_is_validated(mode):
    with pytest.raises((TypeError, ValueError)):
        PdfiumImageExtractor(mode=mode)


def test_rendered_is_the_default_mode(jpeg_pdf):
    [picture] = PdfiumImageExtractor().extract(jpeg_pdf[0]).images
    assert (picture.method, picture.media_type) == ("rendered", "image/png")


def test_both_modes_find_the_same_drawn_pictures(pdf):
    photo = pdf.picture("jpeg")
    pdf.page({"/Photo": photo}, draw("Photo", 10, 300, 80, 60) + draw("Photo", 100, 10, 40, 30))
    data = pdf.save()
    stored = extract(data).images
    rendered = PdfiumImageExtractor(dpi=72).extract(data).images
    assert [(p.method, p.width, p.height) for p in stored] == [("original", 4, 3)] * 2
    # Rendered: the size each copy is drawn at, one point per pixel at 72 DPI.
    assert [(p.method, p.width, p.height) for p in rendered] == [
        ("rendered", 80, 60),
        ("rendered", 40, 30),
    ]


# Which pictures, how many times ------------------------------------------


def test_one_picture_for_every_time_one_is_drawn_in_drawing_order(pdf):
    photo, logo, unused = pdf.picture("jpeg"), pdf.picture("rgb"), pdf.picture("cmyk")
    pdf.page(
        {"/Photo": photo, "/Logo": logo, "/Unused": unused},
        draw("Logo", 10, 300, 40, 40) + draw("Photo", 60, 300, 80, 60)
        + draw("Logo", 150, 300, 20, 20),
    )
    pdf.page({"/Photo": photo}, draw("Photo", 10, 10, 80, 60))
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, None, "image/png", "reencoded", 2, 2),
        (0, None, "image/jpeg", "original", 4, 3),
        (0, None, "image/png", "reencoded", 2, 2),
        (1, None, "image/jpeg", "original", 4, 3),
    ]


def test_inline_images_and_pictures_inside_nested_forms_are_returned(pdf):
    inner = pdf.form({"/Deep": pdf.picture("jpeg")}, draw("Deep", 0, 0, 10, 10))
    outer = pdf.form({"/Inner": inner}, b"/Inner Do")
    pdf.page({"/Outer": outer}, b"/Outer Do\n" + INLINE)
    assert summary(extract(pdf.save())) == [
        (0, None, "image/jpeg", "original", 4, 3),
        (0, None, "image/png", "reencoded", 4, 4),
    ]


def test_pictures_drawn_outside_the_visible_area_are_left_out(pdf):
    picture = pdf.picture("jpeg")
    pdf.page(
        {"/Im": picture},
        draw("Im", 10, 250, 40, 40) + draw("Im", 10, 10, 40, 40),
        crop=[0, 200, 300, 400],
    )
    assert [p.page_index for p in extract(pdf.save()).images] == [0]


def test_document_without_drawn_pictures_is_empty_success(pdf):
    pdf.page({"/Unused": pdf.picture("jpeg")}, b"0 0 m 100 100 l S")
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.images
    assert codes(result) == [("empty", None, None)]


# Formats -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,media_type,method,mode",
    [
        ("jpeg", "image/jpeg", "original", "RGB"),
        ("rgb", "image/png", "reencoded", "RGB"),
        ("cmyk", "image/tiff", "reencoded", "CMYK"),
        ("jpx", "image/png", "reencoded", "RGB"),
    ],
)
def test_formats(pdf, kind, media_type, method, mode):
    pdf.page({"/Im": pdf.picture(kind)}, draw("Im", 10, 10, 50, 50))
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == (media_type, method)
    with Image.open(BytesIO(picture.data)) as image:
        assert image.mode == mode and image.size == (picture.width, picture.height)


def test_masks_are_not_applied_and_not_reported(pdf):
    # PDFium has no way to apply or detect the soft mask: the stored pixels come
    # back opaque, and nothing says a mask was there.
    pdf.page({"/Im": pdf.picture("masked")}, draw("Im", 10, 10, 50, 50))
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.findings
    with Image.open(BytesIO(result.images[0].data)) as image:
        assert image.mode == "RGB"


def test_picture_above_pillows_ceiling_is_skipped(pdf, monkeypatch):
    pdf.page(
        {"/Small": pdf.picture("rgb"), "/Large": pdf.picture("jpeg")},
        draw("Small", 10, 10, 50, 50) + draw("Large", 100, 10, 50, 50),
    )
    data = pdf.save()
    # Ceiling: 2 × 3 = 6 pixels. The 4 × 3 JPEG (12) is above it and skipped;
    # the 2 × 2 picture (4) is not, and opens with Pillow's warning, since it is
    # above the configured limit but below twice that.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 3)
    with pytest.warns(Image.DecompressionBombWarning):
        result = extract(data)
    assert result.outcome == "partial"
    assert codes(result) == [("image_too_large", 0, None)]
    assert summary(result) == [(0, None, "image/png", "reencoded", 2, 2)]
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    assert len(extract(data).images) == 2


def test_unreadable_picture_is_a_finding_and_other_pictures_are_kept(pdf, monkeypatch):
    import pypdfium2 as pdfium

    real = pdfium.PdfImage.extract
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise pdfium.PdfiumError("private backend detail")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfImage, "extract", flaky)
    pdf.page(
        {"/A": pdf.picture("rgb"), "/B": pdf.picture("jpeg")},
        draw("A", 10, 10, 50, 50) + draw("B", 100, 10, 50, 50),
    )
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert codes(result) == [("image_unreadable", 0, None)]
    assert summary(result) == [(0, None, "image/jpeg", "original", 4, 3)]
    assert "private" not in result.model_dump_json()


def test_page_whose_pictures_cannot_be_read_keeps_other_pages(pdf, monkeypatch):
    import pypdfium2 as pdfium

    real = pdfium.PdfPage.get_objects
    pages = []

    def flaky(self, *args, **kwargs):
        if kwargs.get("form") is None:
            pages.append(1)
            if len(pages) == 2:
                raise pdfium.PdfiumError("broken")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "get_objects", flaky)
    for _ in range(3):
        pdf.page({"/Im": pdf.picture("jpeg")}, draw("Im", 10, 10, 50, 50))
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [0, 2]
    assert codes(result) == [("page_unreadable", 1, None)]


# Layout regions ----------------------------------------------------------


@pytest.fixture
def regions_pdf(pdf):
    """Page 1: a photo at the top-left, a logo at the top-right, a bar chart below."""
    pdf.page(
        {"/Photo": pdf.picture("jpeg"), "/Logo": pdf.picture("rgb")},
        draw("Photo", 20, 300, 100, 80)  # displayed (20, 20, 120, 100)
        + draw("Logo", 200, 320, 60, 60)  # displayed (200, 20, 260, 80)
        + b"0 0 1 rg 30 50 40 120 re f\n",  # bars: displayed (30, 230, 70, 350)
    )
    return pdf.save()


PHOTO = ("photo", 1, "image", (20, 20, 120, 100))
LOGO = ("logo", 1, "image", (200, 20, 260, 80))
CHART = ("chart", 1, "unknown", (20, 220, 120, 360))
HEADING = ("heading", 1, "text", (20, 380, 280, 395))


def test_layout_links_each_picture_to_its_image_region(regions_pdf):
    result = extract(regions_pdf, layout=make_layout(PHOTO, LOGO, CHART, HEADING))
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, "photo", "image/jpeg", "original", 4, 3),
        (0, "logo", "image/png", "reencoded", 2, 2),
    ]


def test_explicit_regions_are_used_whatever_their_kind(regions_pdf):
    layout = make_layout(PHOTO, LOGO, CHART, HEADING)
    result = extract(regions_pdf, layout=layout, region_ids=["chart", "logo"])
    assert result.outcome == "success"
    assert summary(result) == [(0, "logo", "image/png", "reencoded", 2, 2)]
    # The chart is drawn with rectangles: no stored picture there.
    assert codes(result) == [("empty", 0, "chart")]


def test_one_region_can_hold_several_pictures(regions_pdf):
    layout = make_layout(("top", 1, "image", (0, 0, 300, 120)))
    assert [p.region_id for p in extract(regions_pdf, layout=layout).images] == [
        "top",
        "top",
    ]


def test_regions_claiming_the_same_picture_are_both_refused(regions_pdf):
    layout = make_layout(PHOTO, ("wide", 1, "image", (0, 0, 150, 150)), LOGO)
    result = extract(regions_pdf, layout=layout)
    assert result.outcome == "partial"
    assert summary(result) == [(0, "logo", "image/png", "reencoded", 2, 2)]
    assert codes(result) == [
        ("selection_overlap", 0, "photo"),
        ("selection_overlap", 0, "wide"),
    ]


def test_rotated_page_regions_use_displayed_coordinates(pdf):
    # The picture sits at PDF (50, 300)-(150, 350); on a page turned a quarter
    # turn clockwise it is displayed at (300, 50)-(350, 150).
    pdf.page({"/Im": pdf.picture("jpeg")}, draw("Im", 50, 300, 100, 50), rotate=90)
    layout = make_layout(
        ("shown", 1, "image", (300, 50, 350, 150)),
        ("unrotated", 1, "image", (50, 50, 150, 100)),
        size=(400, 300),
    )
    result = extract(pdf.save(), layout=layout)
    assert [p.region_id for p in result.images] == ["shown"]
    assert codes(result) == [("empty", 0, "unrotated")]


def test_regions_that_do_not_fit_the_source(regions_pdf):
    frame = LayoutAnalysisResult(
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
    result = extract(regions_pdf, layout=frame)
    assert result.outcome == "partial"
    assert summary(result) == [(0, "photo", "image/jpeg", "original", 4, 3)]
    assert codes(result) == [
        ("page_missing", 1, "gone"),
        ("selection_unsupported", None, "frame"),
    ]
    smaller = make_layout(PHOTO, size=(200, 400))
    assert codes(extract(regions_pdf, layout=smaller)) == [
        ("layout_mismatch", 0, "photo")
    ]
    assert extract(regions_pdf, layout=smaller).outcome == "failed"


def test_nothing_selected_without_opening_the_file(tmp_path):
    missing = tmp_path / "missing.pdf"
    explicit = extract(missing, layout=make_layout(PHOTO), region_ids=[])
    assert explicit.outcome == "success" and codes(explicit) == [
        ("nothing_selected", None, None)
    ]
    no_images = extract(missing, layout=make_layout(HEADING, CHART))
    assert no_images.outcome == "success"
    assert codes(no_images) == [("nothing_selected", None, None)]
    assert explicit.findings[0].message != no_images.findings[0].message


@pytest.mark.parametrize(
    "options,error",
    [
        ({"region_ids": ["photo"]}, ValueError),
        ({"layout": "layout"}, TypeError),
        ({"layout": "fixture", "region_ids": "photo"}, TypeError),
        ({"layout": "fixture", "region_ids": ["nope"]}, ValueError),
    ],
)
def test_invalid_selection_arguments_raise(regions_pdf, options, error):
    if options.get("layout") == "fixture":
        options = {**options, "layout": make_layout(PHOTO)}
    with pytest.raises(error):
        extract(regions_pdf, **options)


# Sources, passwords, and the optional dependency -------------------------


@pytest.fixture
def locked(pdf):
    pdf.page({"/Im": pdf.picture("jpeg")}, draw("Im", 10, 10, 50, 50))
    return pdf.save(password="right-password")


@pytest.mark.parametrize(
    "password,code",
    [(None, "password_required"), ("wrong-password", "invalid_password")],
)
def test_missing_or_wrong_password_fails(locked, password, code):
    result = extract(Artifact(data=locked, password=password))
    assert result.outcome == "failed" and codes(result) == [(code, None, None)]


def test_empty_pdf_after_a_password_failure_is_still_no_pages(locked):
    # PDFium sets no error for a PDF without pages, so its last error would still
    # say "password" here; the reason comes from pikepdf instead.
    import pikepdf

    with pikepdf.Pdf.new() as empty, BytesIO() as output:
        empty.save(output)
        data = output.getvalue()
    assert codes(extract(locked)) == [("password_required", None, None)]
    assert codes(extract(data)) == [("no_pages", None, None)]


def test_correct_password_opens_the_pdf(locked):
    result = extract(Artifact(data=locked, password="right-password"))
    assert len(result.images) == 1
    assert "right-password" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"plain text, not a PDF", "not_applicable", "not_applicable"),
        (b"%PDF-1.7\nbroken beyond repair", "failed", "decode_failed"),
    ],
)
def test_unusable_bytes(data, outcome, code):
    result = extract(data)
    assert result.outcome == outcome and codes(result) == [(code, None, None)]


def test_missing_file_directory_and_empty_pdf(tmp_path, pdf):
    for source in (tmp_path / "missing.pdf", tmp_path):
        assert codes(extract(source)) == [("source_unreadable", None, None)]
    assert codes(extract(pdf.save())) == [("no_pages", None, None)]


def test_missing_pypdfium2_names_the_extra(jpeg_pdf, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    with pytest.raises(ImportError, match=r"raggae\[image-pdf\]"):
        extract(jpeg_pdf[0])
