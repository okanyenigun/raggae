"""Stored PDF pictures page by page with pypdf, inline images included."""

from io import BytesIO

import pikepdf
import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PypdfImageExtractor,
)
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import INLINE, draw, make_layout


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def summary(result):
    return [(p.page_index, p.media_type, p.method) for p in result.images]


def opened(picture):
    image = Image.open(BytesIO(picture.data))
    image.load()
    return image


def extract(data, **options):
    return PypdfImageExtractor().extract(data, **options)


def test_package_and_pdf_family_export_the_same_worker():
    from raggae.documents.image_extractor import pdf as pdf_extractors

    assert pdf_extractors.PypdfImageExtractor is PypdfImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, jpeg_pdf, source_kind):
    data, stored = jpeg_pdf
    path = tmp_path / "manual.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = PypdfImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pypdf"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert picture.data == stored and summary(result) == [(0, "image/jpeg", "original")]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Formats -------------------------------------------------------------------------


def test_original_means_the_stored_bytes(pdf):
    # pypdf passes some JPEGs through and re-encodes others; the label follows
    # the bytes, not the filter.
    small = pdf.picture("jpeg")
    pdf.page({"/Im": small}, draw("Im", 10, 10, 40, 30))
    [picture] = extract(pdf.save()).images
    assert picture.method == "original" and picture.data == small.read_raw_bytes()


@pytest.mark.parametrize(
    "kind,media_type,mode",
    [
        ("rgb", "image/png", "RGB"),
        ("cmyk", "image/tiff", "CMYK"),
        ("separation", "image/tiff", "CMYK"),
        ("jpx", "image/png", "RGB"),
        ("masked", "image/png", "RGBA"),
    ],
)
def test_decoded_pictures_become_new_files(pdf, kind, media_type, mode):
    pdf.page({"/Im": pdf.picture(kind)}, draw("Im", 10, 10, 40, 30))
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == (media_type, "reencoded")
    assert opened(picture).mode == mode


def test_soft_mask_is_applied_as_transparency(pdf):
    pdf.page({"/Im": pdf.picture("masked")}, draw("Im", 10, 10, 40, 30))
    [picture] = extract(pdf.save()).images
    assert opened(picture).getchannel("A").tobytes() == bytes([0, 255, 128, 64])


def test_unreadable_and_unsupported_pictures_keep_the_others(pdf, monkeypatch):
    pdf.page({"/A": pdf.picture("corrupt"), "/B": pdf.picture("jpeg")})
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert codes(result) == [("image_unreadable", 0)]

    from pypdf._page import VirtualListImages

    def unsupported(self, index):
        raise NotImplementedError("private decoder detail")

    monkeypatch.setattr(VirtualListImages, "__getitem__", unsupported)
    blocked = extract(pdf.save())
    assert [code for code, _ in codes(blocked)] == ["unsupported_image_type"] * 2
    assert "private" not in blocked.model_dump_json()


# Which pictures, how many times ------------------------------------------------


def test_inline_images_and_pictures_inside_forms_are_returned(pdf):
    inner = pdf.form({"/Deep": pdf.picture("jpeg")}, draw("Deep", 0, 0, 10, 10))
    pdf.page({"/Fm": inner}, b"/Fm Do\n" + INLINE)
    assert summary(extract(pdf.save())) == [
        (0, "image/jpeg", "original"),
        (0, "image/png", "reencoded"),
    ]


def test_each_picture_once_for_every_page_that_lists_it(pdf):
    logo, unused = pdf.picture("jpeg"), pdf.picture("rgb")
    pdf.page({"/Logo": logo, "/Again": logo, "/Unused": unused}, b"/Logo Do")
    pdf.page({"/Logo": logo}, b"/Logo Do")
    result = extract(pdf.save())
    assert result.outcome == "success"
    assert sorted(summary(result)) == [
        (0, "image/jpeg", "original"),
        (0, "image/png", "reencoded"),
        (1, "image/jpeg", "original"),
    ]


def test_document_without_pictures_is_empty_success(pdf):
    pdf.page(content=b"0 0 m 100 100 l S")
    result = extract(pdf.save())
    assert result.outcome == "success" and codes(result) == [("empty", None)]


# Size and pages ------------------------------------------------------------------


def test_picture_above_pillows_ceiling_is_skipped_before_decoding(pdf, monkeypatch):
    pdf.page(
        {
            "/Huge": pdf.picture("rgb", Width=20_000, Height=20_000),
            "/Small": pdf.picture("jpeg"),
        }
    )
    data = pdf.save()
    result = extract(data)
    assert result.outcome == "partial"
    assert summary(result) == [(0, "image/jpeg", "original")]
    assert codes(result) == [("image_too_large", 0)]
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)  # the 4 × 3 JPEG is now too large
    assert codes(extract(data)) == [("image_too_large", 0), ("image_too_large", 0)]


def test_page_whose_pictures_cannot_be_listed_keeps_other_pages(pdf, monkeypatch):
    from pypdf import PageObject
    from pypdf.errors import PdfReadError

    real = PageObject.images.fget
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 2:
            raise PdfReadError("private backend detail")
        return real(self)

    monkeypatch.setattr(PageObject, "images", property(flaky))
    for _ in range(3):
        pdf.page({"/Im": pdf.picture("jpeg")})
    result = extract(pdf.save())
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [0, 2]
    assert codes(result) == [("page_unreadable", 1)]


# Sources and passwords -------------------------------------------------------------


@pytest.fixture
def locked(pdf):
    pdf.page({"/Im": pdf.picture("jpeg")})
    return pdf.save(password="right-password")


def test_passwords(locked):
    assert codes(extract(locked)) == [("password_required", None)]
    wrong = extract(Artifact(data=locked, password="wrong-password"))
    assert codes(wrong) == [("invalid_password", None)]
    right = extract(Artifact(data=locked, password="right-password"))
    assert summary(right) == [(0, "image/jpeg", "original")]
    assert "right-password" not in right.model_dump_json()


def test_unusable_sources(tmp_path):
    with pikepdf.Pdf.new() as empty, BytesIO() as output:
        empty.save(output)
        no_pages = output.getvalue()
    assert codes(extract(no_pages)) == [("no_pages", None)]
    assert codes(extract(b"")) == [("decode_failed", None)]
    assert extract(b"plain text, not a PDF").outcome == "not_applicable"
    assert codes(extract(b"%PDF-1.7\nbroken beyond repair")) == [("decode_failed", None)]
    assert codes(extract(tmp_path / "missing.pdf")) == [("source_unreadable", None)]


def test_layouts_are_refused_without_opening_the_file(tmp_path):
    layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    missing = tmp_path / "missing.pdf"
    result = extract(missing, layout=layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "photo")
    ]
    empty = extract(missing, layout=layout, region_ids=[])
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]
