"""Stored PDF pictures page by page with pikepdf; whole pages only."""

import warnings
from io import BytesIO

import pikepdf
import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PikepdfImageExtractor,
)
from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact


def codes(result):
    return [(finding.code, finding.page_index) for finding in result.findings]


def summary(result):
    return [
        (picture.page_index, picture.media_type, picture.method)
        for picture in result.images
    ]


def opened(picture):
    image = Image.open(BytesIO(picture.data))
    image.load()
    return image


def extract(data, **options):
    return PikepdfImageExtractor().extract(data, **options)


@pytest.fixture
def layout():
    return LayoutAnalysisResult(
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
                id="page-1-r1", surface_id="page-1", kind="image", bbox=(10, 10, 50, 40)
            ),
            LayoutRegion(
                id="page-1-r2", surface_id="page-1", kind="text", bbox=(60, 10, 200, 40)
            ),
        ),
    )


def test_package_and_pdf_family_export_the_same_worker():
    from raggae.documents.image_extractor import pdf as pdf_extractors

    assert pdf_extractors.PikepdfImageExtractor is PikepdfImageExtractor


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, jpeg_pdf, source_kind, keyword
):
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
    extractor = PikepdfImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pikepdf"
    result = extractor.extract(source=source) if keyword else extractor.extract(source)
    assert result.extractor == "image_extractor_pikepdf"
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert picture.data == stored
    assert (picture.media_type, picture.width, picture.height) == ("image/jpeg", 4, 3)
    assert (picture.page_index, picture.region_id, picture.alt_text) == (0, None, None)
    assert picture.method == "original"
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Formats -----------------------------------------------------------------


def test_raw_pixels_become_a_png(pdf):
    pdf.page({"/Im": pdf.picture("rgb")})
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == ("image/png", "reencoded")
    image = opened(picture)
    assert image.size == (2, 2) and image.getpixel((1, 1)) == (200, 30, 30)


def test_cmyk_pixels_become_a_cmyk_tiff_rather_than_converted_colours(pdf):
    pdf.page({"/Im": pdf.picture("cmyk")})
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == ("image/tiff", "reencoded")
    image = opened(picture)
    assert image.mode == "CMYK" and image.getpixel((0, 0)) == (0, 100, 200, 10)


def test_jpeg_2000_is_saved_as_a_png(pdf):
    pdf.page({"/Im": pdf.picture("jpx")})
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == ("image/png", "reencoded")
    assert (picture.width, picture.height) == opened(picture).size == (5, 4)


def test_soft_mask_is_applied_as_transparency(pdf):
    pdf.page({"/Im": pdf.picture("masked")})
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == ("image/png", "reencoded")
    image = opened(picture)
    assert image.mode == "RGBA"
    assert image.getchannel("A").tobytes() == bytes([0, 255, 128, 64])


def test_unsupported_encoding_is_a_finding_and_other_pictures_are_kept(pdf):
    pdf.page({"/A": pdf.picture("separation"), "/B": pdf.picture("jpeg")})
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert codes(result) == [("unsupported_image_type", 0)]


def test_unreadable_picture_is_a_finding_without_backend_detail(pdf):
    pdf.page({"/A": pdf.picture("corrupt"), "/B": pdf.picture("rgb")})
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert summary(result) == [(0, "image/png", "reencoded")]
    assert codes(result) == [("image_unreadable", 0)]
    assert "zlib" not in result.model_dump_json()


# Which pictures, how many times ------------------------------------------


def test_each_picture_once_for_every_page_that_lists_it(pdf):
    logo, photo, leftover = pdf.picture("jpeg"), pdf.picture("rgb"), pdf.picture("cmyk")
    pdf.page({"/Logo": logo, "/Photo": photo, "/Again": photo}, b"/Photo Do /Again Do")
    pdf.page({"/Logo": logo, "/Unused": leftover}, b"/Logo Do")
    pdf.page({"/Logo": logo}, b"/Logo Do")
    result = extract(pdf.save())
    assert result.outcome == "success"
    # Within a page, pikepdf's order: sorted by resource name, so the photo
    # (first listed as /Again) comes before /Logo.
    assert summary(result) == [
        (0, "image/png", "reencoded"),
        (0, "image/jpeg", "original"),
        (1, "image/jpeg", "original"),
        (1, "image/tiff", "reencoded"),
        (2, "image/jpeg", "original"),
    ]


def test_pictures_inside_nested_and_cyclic_forms(pdf):
    inner = pdf.form({"/Deep": pdf.picture("jpeg")}, b"/Deep Do")
    outer = pdf.form({"/Inner": inner}, b"/Inner Do")
    loop = pdf.form(content=b"/Loop Do")
    loop.Resources.XObject.Loop = loop
    pdf.page({"/Outer": outer, "/Loop": loop}, b"/Outer Do /Loop Do")
    result = extract(pdf.save())
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert result.outcome == "success"


def test_document_without_pictures_is_empty_success(pdf):
    pdf.page(content=b"0 0 m 100 100 l S")
    pdf.page()
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.images
    assert codes(result) == [("empty", None)]


# Inline images -----------------------------------------------------------

INLINE = b"BI /W 4 /H 4 /BPC 8 /CS /G ID " + bytes(16) + b" EI"


def test_inline_images_are_reported_not_extracted(pdf):
    pdf.page({"/Im": pdf.picture("rgb")}, b"q " + INLINE + b" Q")
    pdf.page()
    pdf.page({"/Fm": pdf.form(content=INLINE)}, b"/Fm Do")
    result = extract(pdf.save())
    assert result.outcome == "success"
    assert summary(result) == [(0, "image/png", "reencoded")]
    assert codes(result) == [
        ("inline_images_skipped", 0),
        ("inline_images_skipped", 2),
    ]


def test_inline_images_alone_are_reported_with_an_empty_result(pdf):
    pdf.page(content=INLINE)
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.images
    assert codes(result) == [("inline_images_skipped", 0), ("empty", None)]


def test_drawing_commands_that_cannot_be_checked_keep_the_pictures(pdf, monkeypatch):
    def broken(*args, **kwargs):
        raise pikepdf.PdfError("private parser detail")

    monkeypatch.setattr(pikepdf, "parse_content_stream", broken)
    pdf.page({"/Im": pdf.picture("rgb")})
    result = extract(pdf.save())
    assert result.outcome == "success"
    assert summary(result) == [(0, "image/png", "reencoded")]
    assert codes(result) == [("inline_images_skipped", 0)]
    assert "private" not in result.model_dump_json()


# Decompression-bomb ceiling ----------------------------------------------


def test_picture_above_pillows_ceiling_is_skipped_before_decoding(pdf):
    pdf.page(
        {
            "/Huge": pdf.picture("rgb", Width=20_000, Height=20_000),
            "/Small": pdf.picture("jpeg"),
        }
    )
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert codes(result) == [("image_too_large", 0)]


def test_pillows_configured_ceiling_is_the_one_applied(pdf, monkeypatch):
    pdf.page({"/Im": pdf.picture("rgb")})
    data = pdf.save()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    assert codes(extract(data)) == [("image_too_large", 0)]
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    assert summary(extract(data)) == [(0, "image/png", "reencoded")]


# Pages -------------------------------------------------------------------


def test_page_whose_pictures_cannot_be_listed_keeps_other_pages(pdf, monkeypatch):
    real = pikepdf.Page.get_images
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise pikepdf.PdfError("private backend detail")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pikepdf.Page, "get_images", flaky)
    for _ in range(3):
        pdf.page({"/Im": pdf.picture("jpeg")})
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert [picture.page_index for picture in result.images] == [0, 2]
    assert codes(result) == [("page_unreadable", 1)]
    assert "private" not in result.model_dump_json()


def test_all_pages_unreadable_fails(pdf, monkeypatch):
    def broken(self, *args, **kwargs):
        raise pikepdf.PdfError("broken")

    monkeypatch.setattr(pikepdf.Page, "get_images", broken)
    pdf.page({"/Im": pdf.picture("jpeg")})
    result = extract(pdf.save())
    assert result.outcome == "failed" and not result.images
    assert codes(result) == [("page_unreadable", 0)]


def test_pdf_without_pages_fails(pdf):
    result = extract(pdf.save())
    assert result.outcome == "failed"
    assert codes(result) == [("no_pages", None)]


# Passwords ---------------------------------------------------------------


@pytest.fixture
def locked(pdf):
    pdf.page({"/Im": pdf.picture("jpeg")})
    return pdf.save(password="right-password")


def test_password_for_an_unencrypted_pdf_raises_no_warning(jpeg_pdf):
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        result = extract(Artifact(data=jpeg_pdf[0], password="unneeded-password"))
    assert result.outcome == "success" and len(result.images) == 1


def test_correct_password_opens_the_pdf(locked):
    result = extract(Artifact(data=locked, password="right-password"))
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert "right-password" not in result.model_dump_json()


@pytest.mark.parametrize(
    "source,code",
    [
        ("no_password", "password_required"),
        ("wrong_password", "invalid_password"),
    ],
)
def test_missing_or_wrong_password_fails(locked, source, code):
    source = {
        "no_password": locked,
        "wrong_password": Artifact(data=locked, password="wrong-password"),
    }[source]
    result = extract(source)
    assert result.outcome == "failed" and codes(result) == [(code, None)]
    assert "wrong-password" not in result.model_dump_json()


# Unreadable and unsupported sources --------------------------------------


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
    assert result.outcome == outcome and not result.images
    assert codes(result) == [(code, None)]


def test_missing_file_and_directory_are_unreadable(tmp_path):
    for source in (tmp_path / "missing.pdf", tmp_path):
        result = extract(source)
        assert result.outcome == "failed"
        assert codes(result) == [("source_unreadable", None)]


@pytest.mark.parametrize("source", [None, 1, bytearray(b"%PDF-")])
def test_invalid_source_type_raises(source):
    with pytest.raises(TypeError):
        extract(source)


# Layout regions ----------------------------------------------------------


def test_layout_regions_are_refused_without_opening_the_source(tmp_path, layout):
    missing = tmp_path / "missing.pdf"
    result = extract(missing, layout=layout, region_ids=["page-1-r2", "page-1-r1"])
    assert result.outcome == "failed" and not result.images
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "page-1-r2"),
        ("selection_unsupported", "page-1-r1"),
    ]
    every = extract(missing, layout=layout)
    assert [f.region_id for f in every.findings] == ["page-1-r1", "page-1-r2"]


def test_empty_selection_is_refused_with_one_finding(jpeg_pdf, layout):
    result = extract(jpeg_pdf[0], layout=layout, region_ids=())
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize(
    "options,error",
    [
        ({"region_ids": ["page-1-r1"]}, ValueError),
        ({"layout": "layout"}, TypeError),
        ({"layout": "fixture", "region_ids": "page-1-r1"}, TypeError),
        ({"layout": "fixture", "region_ids": ["page-9-r9"]}, ValueError),
        ({"layout": "fixture", "region_ids": ["page-1-r1", "page-1-r1"]}, ValueError),
    ],
)
def test_invalid_selection_arguments_raise(jpeg_pdf, layout, options, error):
    if options.get("layout") == "fixture":
        options = {**options, "layout": layout}
    with pytest.raises(error):
        extract(jpeg_pdf[0], **options)
