"""Stored PDF pictures per drawn occurrence with PyMuPDF, standalone or by region."""

import sys
from io import BytesIO

import pikepdf
import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PyMuPdfImageExtractor,
)
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import INLINE, draw, make_layout


@pytest.fixture(autouse=True)
def backend():
    # PyMuPDF is an opt-in extra (AGPL); without it these tests are skipped.
    return pytest.importorskip("pymupdf", reason="Install raggae[text-pymupdf]")


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [(p.page_index, p.region_id, p.media_type, p.method) for p in result.images]


def opened(picture):
    image = Image.open(BytesIO(picture.data))
    image.load()
    return image


def extract(data, **options):
    return PyMuPdfImageExtractor().extract(data, **options)


def test_package_and_pdf_family_export_the_same_worker():
    from raggae.documents.image_extractor import pdf as pdf_extractors

    assert pdf_extractors.PyMuPdfImageExtractor is PyMuPdfImageExtractor


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
    extractor = PyMuPdfImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pymupdf"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert picture.data == stored and summary(result) == [(0, None, "image/jpeg", "original")]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Which pictures, how many times ------------------------------------------------


def test_one_picture_per_drawn_occurrence(pdf):
    photo, unused = pdf.picture("jpeg"), pdf.picture("cmyk")
    pdf.page(
        {"/Photo": photo, "/Unused": unused},
        draw("Photo", 10, 300, 80, 60) + draw("Photo", 100, 300, 40, 30),
    )
    pdf.page({"/Photo": photo}, draw("Photo", 10, 10, 80, 60))
    result = extract(pdf.save())
    assert result.outcome == "success" and not result.findings
    assert [p.page_index for p in result.images] == [0, 0, 1]


def test_pictures_inside_forms_and_inline_images(pdf):
    inner = pdf.form({"/Deep": pdf.picture("jpeg")}, draw("Deep", 0, 0, 10, 10))
    pdf.page({"/Fm": inner}, b"/Fm Do\n" + INLINE)
    result = extract(pdf.save())
    assert summary(result) == [(0, None, "image/jpeg", "original")]
    assert codes(result) == [("inline_images_skipped", 0, None)]


def test_pictures_drawn_outside_the_visible_area_are_left_out(pdf):
    pdf.page(
        {"/Im": pdf.picture("jpeg")},
        draw("Im", 10, 250, 40, 40) + draw("Im", 10, 10, 40, 40),
        crop=[0, 200, 300, 400],
    )
    assert len(extract(pdf.save()).images) == 1


def test_document_without_drawn_pictures_is_empty(pdf):
    pdf.page({"/Unused": pdf.picture("jpeg")}, b"0 0 m 100 100 l S")
    result = extract(pdf.save())
    assert result.outcome == "success" and codes(result) == [("empty", None, None)]


# Formats -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,media_type,method,mode",
    [
        ("jpeg", "image/jpeg", "original", "RGB"),
        ("rgb", "image/png", "reencoded", "RGB"),
        ("cmyk", "image/png", "reencoded", "RGB"),  # PyMuPDF converts CMYK to RGB
        ("separation", "image/png", "reencoded", "RGB"),
        ("jpx", "image/png", "reencoded", "RGB"),
    ],
)
def test_formats(pdf, kind, media_type, method, mode):
    pdf.page({"/Im": pdf.picture(kind)}, draw("Im", 10, 10, 40, 30))
    [picture] = extract(pdf.save()).images
    assert (picture.media_type, picture.method) == (media_type, method)
    assert opened(picture).mode == mode


def test_soft_masks_are_applied(pdf):
    pdf.page({"/Im": pdf.picture("masked")}, draw("Im", 10, 10, 40, 30))
    [picture] = extract(pdf.save()).images
    image = opened(picture)
    assert (picture.media_type, image.mode) == ("image/png", "RGBA")
    assert image.getchannel("A").tobytes() == bytes([0, 255, 128, 64])


def test_picture_above_pillows_ceiling_is_skipped(pdf, monkeypatch):
    pdf.page(
        {"/Small": pdf.picture("rgb"), "/Large": pdf.picture("jpeg")},
        draw("Small", 10, 10, 40, 30) + draw("Large", 100, 10, 40, 30),
    )
    data = pdf.save()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 3)  # ceiling: 6 pixels
    with pytest.warns(Image.DecompressionBombWarning):
        result = extract(data)
    assert result.outcome == "partial"
    assert summary(result) == [(0, None, "image/png", "reencoded")]
    assert codes(result) == [("image_too_large", 0, None)]


def test_failures_keep_the_other_pictures_and_pages(pdf, monkeypatch, backend):
    pymupdf = backend
    real_extract = pymupdf.Document.extract_image
    real_info = pymupdf.Page.get_image_info
    pages = []

    def broken_picture(self, xref):
        if xref == first_xref:
            raise RuntimeError("private backend detail")
        return real_extract(self, xref)

    def broken_page(self, *args, **kwargs):
        pages.append(1)
        if len(pages) == 2:
            raise RuntimeError("private backend detail")
        return real_info(self, *args, **kwargs)

    first, second = pdf.picture("rgb"), pdf.picture("jpeg")
    for _ in range(3):
        pdf.page({"/A": first, "/B": second}, draw("A", 10, 10, 40, 30) + draw("B", 100, 10, 40, 30))
    data = pdf.save()
    with pymupdf.open(stream=data, filetype="pdf") as document:
        first_xref = document[0].get_image_info(xrefs=True)[0]["xref"]
    monkeypatch.setattr(pymupdf.Document, "extract_image", broken_picture)
    monkeypatch.setattr(pymupdf.Page, "get_image_info", broken_page)
    result = extract(data)
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [0, 2]
    assert codes(result) == [
        ("image_unreadable", 0, None),
        ("page_unreadable", 1, None),
        ("image_unreadable", 2, None),
    ]
    assert "private" not in result.model_dump_json()


# Layout regions --------------------------------------------------------------------


@pytest.fixture
def regions_pdf(pdf):
    pdf.page(
        {"/Photo": pdf.picture("jpeg"), "/Logo": pdf.picture("rgb")},
        draw("Photo", 20, 300, 100, 80)  # displayed (20, 20, 120, 100)
        + draw("Logo", 200, 320, 60, 60)  # displayed (200, 20, 260, 80)
        + b"0 0 1 rg 30 50 40 120 re f\n"
        + INLINE,  # inline image: displayed (200, 300, 240, 340)
    )
    return pdf.save()


PHOTO = ("photo", 1, "image", (20, 20, 120, 100))
LOGO = ("logo", 1, "image", (200, 20, 260, 80))
CHART = ("chart", 1, "unknown", (20, 220, 120, 360))
INLINE_REGION = ("inline", 1, "image", (190, 290, 250, 350))


def test_layout_links_pictures_and_reports_inline_ones(regions_pdf):
    result = extract(regions_pdf, layout=make_layout(PHOTO, LOGO, CHART, INLINE_REGION))
    assert result.outcome == "success"
    assert summary(result) == [
        (0, "photo", "image/jpeg", "original"),
        (0, "logo", "image/png", "reencoded"),
    ]
    assert codes(result) == [("inline_images_skipped", 0, "inline")]


def test_explicit_regions_several_pictures_and_overlaps(regions_pdf):
    layout = make_layout(PHOTO, CHART, ("top", 1, "image", (0, 0, 300, 120)))
    explicit = extract(regions_pdf, layout=layout, region_ids=["chart", "top"])
    assert [p.region_id for p in explicit.images] == ["top", "top"]
    assert codes(explicit) == [("empty", 0, "chart")]
    overlapping = extract(regions_pdf, layout=layout, region_ids=["photo", "top"])
    assert overlapping.outcome == "failed"
    assert codes(overlapping) == [
        ("selection_overlap", 0, "photo"),
        ("selection_overlap", 0, "top"),
    ]


@pytest.mark.parametrize("on_tree", [False, True])
def test_rotated_pages_including_rotation_inherited_from_the_page_tree(pdf, on_tree):
    pdf.page({"/Im": pdf.picture("jpeg")}, draw("Im", 50, 300, 100, 50))
    if on_tree:
        # The builder writes /Rotate 0 on each page, which would override the tree's.
        del pdf.document.pages[0].obj["/Rotate"]
        pdf.document.Root.Pages.Rotate = 90
    else:
        pdf.document.pages[0].Rotate = 90
    layout = make_layout(("shown", 1, "image", (300, 50, 350, 150)), size=(400, 300))
    result = extract(pdf.save(), layout=layout)
    assert [p.region_id for p in result.images] == ["shown"]


def test_regions_that_do_not_fit_and_empty_selections(tmp_path, regions_pdf):
    gone = make_layout(PHOTO, ("gone", 2, "image", (0, 0, 9, 9)), pages=2)
    result = extract(regions_pdf, layout=gone)
    assert codes(result) == [("page_missing", 1, "gone")]
    smaller = extract(regions_pdf, layout=make_layout(PHOTO, size=(200, 400)))
    assert smaller.outcome == "failed" and codes(smaller) == [("layout_mismatch", 0, "photo")]
    missing = tmp_path / "missing.pdf"
    assert codes(extract(missing, layout=make_layout(PHOTO), region_ids=[])) == [
        ("nothing_selected", None, None)
    ]


# Sources, passwords, and the optional dependency -------------------------------------


def test_passwords(pdf):
    pdf.page({"/Im": pdf.picture("jpeg")}, draw("Im", 10, 10, 40, 30))
    locked = pdf.save(password="right-password")
    assert codes(extract(locked)) == [("password_required", None, None)]
    assert codes(extract(Artifact(data=locked, password="wrong"))) == [
        ("invalid_password", None, None)
    ]
    right = extract(Artifact(data=locked, password="right-password"))
    assert len(right.images) == 1 and "right-password" not in right.model_dump_json()


def test_unusable_sources(tmp_path):
    with pikepdf.Pdf.new() as empty, BytesIO() as output:
        empty.save(output)
        no_pages = output.getvalue()
    assert codes(extract(no_pages)) == [("no_pages", None, None)]
    assert codes(extract(b"")) == [("decode_failed", None, None)]
    assert extract(b"plain text, not a PDF").outcome == "not_applicable"
    assert codes(extract(b"%PDF-1.7\nbroken beyond repair")) == [("decode_failed", None, None)]
    assert codes(extract(tmp_path / "missing.pdf")) == [("source_unreadable", None, None)]


def test_missing_pymupdf_names_the_extra_and_licence(jpeg_pdf, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    with pytest.raises(ImportError, match=r"raggae\[text-pymupdf\].*AGPL"):
        extract(jpeg_pdf[0])
