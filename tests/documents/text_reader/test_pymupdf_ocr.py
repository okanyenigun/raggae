"""PyMuPdfOcrTextReader: whole-page OCR through PyMuPDF's built-in Tesseract."""

import os
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    NativePdfLayoutAnalyzer,
)
from raggae.documents.layout_analysis.models import (
    DocxBodyLocation,
    DocxElementLocation,
    PdfPageLocation,
)
from raggae.documents.text_reader import (
    PyMuPdfOcrTextReader,
    PyMuPdfTextReader,
    TextReader,
    TextReadResult,
)
from raggae.documents.text_reader.pdf import PyMuPdfOcrTextReader as FromPdf

TESSDATA = Path(os.environ.get("RAGGAE_TESSDATA", "/opt/homebrew/share/tessdata"))

needs_tessdata = pytest.mark.skipif(
    not (TESSDATA / "eng.traineddata").is_file(),
    reason="Tesseract English data not found (set RAGGAE_TESSDATA)",
)

# Laid out in displayed coordinates (y up) so it fits every rotation (400x300).
REPORT = (
    b"BT /F1 14 Tf 30 250 Td (Purchase report) Tj ET\n"
    b"BT /F1 12 Tf 30 200 Td (Shipping labels) Tj 0 -16 Td (Cost: 90 USD) Tj ET\n"
    b"BT /F1 12 Tf 180 200 Td (Packing tape) Tj 0 -16 Td (Cost: 18 USD) Tj ET"
)
REGIONS = [
    "Purchase report",
    "Shipping labels\nCost: 90 USD",
    "Packing tape\nCost: 18 USD",
]

# Maps displayed coordinates to the unrotated 300x400 page, so text is upright
# as displayed (as on a scan whose /Rotate straightens it).
UPRIGHT = {
    0: b"",
    90: b"0 1 -1 0 300 0 cm\n",
    180: b"-1 0 0 -1 300 400 cm\n",
    270: b"0 -1 1 0 0 400 cm\n",
}


def upright(content, rotation):
    return b"q\n" + UPRIGHT[rotation] + content + b"\nQ"


def text_page(text):
    return b"BT /F1 12 Tf 30 330 Td (" + text.encode() + b") Tj ET"


def scanned_pdf(lines):
    image = Image.new("RGB", (1000, 300), "white")
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(lines):
        draw.text(
            (40, 40 + number * 100),
            line,
            fill="black",
            font=ImageFont.load_default(size=44),
        )
    buffer = BytesIO()
    image.save(buffer, "PDF", resolution=150)
    return buffer.getvalue()


@pytest.fixture
def reader():
    pytest.importorskip("pymupdf", reason="Install raggae[text-pymupdf]")
    if not (TESSDATA / "eng.traineddata").is_file():
        pytest.skip("Tesseract English data not found (set RAGGAE_TESSDATA)")
    return PyMuPdfOcrTextReader(language="eng", tessdata=TESSDATA)


def layout_for(regions, *, width=300, height=400):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=width,
                height=height,
            ),
        ),
        regions=tuple(
            LayoutRegion(id=identifier, surface_id="page-1", kind=kind, bbox=bbox)
            for identifier, kind, bbox in regions
        ),
    )


# Configuration ------------------------------------------------------------------


def tessdata_folder(tmp_path, *codes):
    for code in codes:
        (tmp_path / f"{code}.traineddata").write_bytes(b"")
    return tmp_path


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"language": None}, TypeError),
        ({"language": ""}, ValueError),
        ({"language": "eng deu"}, ValueError),
        ({"language": "../eng"}, ValueError),
        ({"language": "deu"}, ValueError),  # Not in the folder.
        ({"language": "eng+deu"}, ValueError),
        ({"tessdata": 3}, TypeError),
        ({"tessdata": "/no/such/tessdata"}, ValueError),
        ({"dpi": 0}, ValueError),
        ({"dpi": True}, ValueError),
        ({"dpi": float("nan")}, ValueError),
        ({"dpi": "300"}, ValueError),
    ],
)
def test_configuration_is_validated(tmp_path, kwargs, error):
    base = {"language": "eng", "tessdata": tessdata_folder(tmp_path, "eng")}
    with pytest.raises(error):
        PyMuPdfOcrTextReader(**{**base, **kwargs})


def test_language_and_tessdata_are_required():
    with pytest.raises(TypeError):
        PyMuPdfOcrTextReader()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        PyMuPdfOcrTextReader(language="eng")  # type: ignore[call-arg]


def test_a_file_is_not_a_tessdata_folder(tmp_path):
    file = tmp_path / "eng.traineddata"
    file.write_bytes(b"")
    with pytest.raises(ValueError, match="tessdata folder"):
        PyMuPdfOcrTextReader(language="eng", tessdata=file)


def test_missing_language_data_is_never_downloaded(tmp_path):
    folder = tessdata_folder(tmp_path, "eng")
    with pytest.raises(ValueError, match="'deu'.*never downloaded"):
        PyMuPdfOcrTextReader(language="eng+deu", tessdata=folder)
    assert PyMuPdfOcrTextReader(language="eng", tessdata=str(folder)).language == "eng"


def test_names_exports_and_protocol(tmp_path):
    reader = PyMuPdfOcrTextReader(
        language="eng", tessdata=tessdata_folder(tmp_path, "eng")
    )
    assert FromPdf is PyMuPdfOcrTextReader
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pymupdf_ocr"
    assert PyMuPdfTextReader().name == "text_reader_pymupdf"


# Recognition ---------------------------------------------------------------------


@needs_tessdata
def test_recognizes_a_scanned_page_that_has_no_stored_text(reader):
    data = scanned_pdf(["Delivery note 2026-118", "Shipping labels: 600"])
    assert PyMuPdfTextReader().read(data).blocks == ()
    result = reader.read(data)
    assert isinstance(result, TextReadResult) and result.outcome == "success"
    assert [(b.page_index, b.text, b.method, b.region_id) for b in result.blocks] == [
        (0, "Delivery note 2026-118\n\nShipping labels: 600", "ocr", None)
    ]
    assert result.reader == "text_reader_pymupdf_ocr"


@needs_tessdata
def test_stored_text_is_ignored_only_visible_pixels_count(reader, make_reader_pdf):
    # Render mode 3 stores invisible text, as an earlier OCR layer would.
    data = make_reader_pdf(
        (
            b"BT 3 Tr /F1 12 Tf 30 300 Td (Hidden layer) Tj ET\n"
            b"BT 0 Tr /F1 14 Tf 30 330 Td (Visible words) Tj ET",
        )
    )
    assert [b.text for b in PyMuPdfTextReader().read(data).blocks] == [
        "Hidden layer\n\nVisible words"
    ]
    assert [b.text for b in reader.read(data).blocks] == ["Visible words"]


@needs_tessdata
def test_annotations_are_not_rendered(reader, make_reader_pdf):
    import pymupdf

    document = pymupdf.open(stream=make_reader_pdf((text_page("Body text"),)))
    document[0].add_freetext_annot(
        pymupdf.Rect(30, 30, 250, 60), "Reviewer stamp", fontsize=14
    )
    data = document.tobytes()
    document.close()
    assert [b.text for b in reader.read(data).blocks] == ["Body text"]


@needs_tessdata
def test_blank_pages_are_reported_empty_and_others_kept(reader, make_reader_pdf):
    data = make_reader_pdf((text_page("Page one"), b"", text_page("Page three")))
    result = reader.read(data)
    assert result.outcome == "success"
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (0, "Page one"),
        (2, "Page three"),
    ]
    assert [(f.code, f.page_index) for f in result.findings] == [("empty", 1)]


@needs_tessdata
def test_fractional_dpi_is_accepted(make_reader_pdf):
    pytest.importorskip("pymupdf", reason="Install raggae[text-pymupdf]")
    reader =PyMuPdfOcrTextReader(language="eng", tessdata=TESSDATA, dpi=250.5)
    data = make_reader_pdf((text_page("Fractional resolution"),))
    assert [b.text for b in reader.read(data).blocks] == ["Fractional resolution"]


@needs_tessdata
def test_no_tesseract_lookup_or_environment_is_used(
    reader, make_reader_pdf, monkeypatch
):
    def no_shell(*args, **kwargs):
        raise AssertionError("must not run commands to locate Tesseract")

    monkeypatch.setattr(subprocess, "run", no_shell)
    monkeypatch.setenv("TESSDATA_PREFIX", "/no/such/tessdata")
    data = make_reader_pdf((text_page("Configured folder"),))
    assert [b.text for b in reader.read(data).blocks] == ["Configured folder"]


@needs_tessdata
def test_ocr_failure_on_one_page_keeps_the_others(reader, make_reader_pdf, monkeypatch):
    import pymupdf
    from pymupdf.mupdf import FzErrorLibrary

    real = pymupdf.Pixmap.pdfocr_tobytes
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise FzErrorLibrary("private backend detail")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Pixmap, "pdfocr_tobytes", flaky)
    data = make_reader_pdf((text_page("One"), text_page("Two"), text_page("Three")))
    result = reader.read(data)
    assert result.outcome == "partial"
    assert [(b.page_index, b.text) for b in result.blocks] == [(0, "One"), (2, "Three")]
    assert [(f.code, f.page_index) for f in result.findings] == [("page_unreadable", 1)]
    assert "private" not in result.model_dump_json()


# Regions -------------------------------------------------------------------------


@needs_tessdata
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"rotation": 90},
        {"rotation": 180},
        {"rotation": 270},
        {"rotation": 90, "unit": 2},
        {"rotation": 270, "crop": (10, 20, 290, 390)},
        {"rotation": 180, "crop": (10, 20, 290, 390), "unit": 2},
    ],
)
def test_regions_match_the_native_reader_on_rotated_cropped_scaled_pages(
    reader, make_reader_pdf, options
):
    data = make_reader_pdf((upright(REPORT, options.get("rotation", 0)),), **options)
    layout = NativePdfLayoutAnalyzer().analyze(data)
    native = PyMuPdfTextReader().read(data, layout=layout)
    result = reader.read(data, layout=layout)
    assert result.outcome == "success", result.findings
    assert [b.text for b in result.blocks] == REGIONS
    assert [b.region_id for b in result.blocks] == [b.region_id for b in native.blocks]
    assert {b.method for b in result.blocks} == {"ocr"}


@needs_tessdata
def test_selected_regions_only_and_image_regions_are_read(reader, make_reader_pdf):
    data = make_reader_pdf((upright(REPORT, 0),))
    layout = layout_for(
        [
            ("title", "text", (20, 135, 200, 160)),
            ("picture", "image", (170, 185, 290, 230)),
        ]
    )
    result = reader.read(data, layout=layout, region_ids=["picture"])
    assert [(b.region_id, b.text) for b in result.blocks] == [
        ("picture", "Packing tape\nCost: 18 USD")
    ]


@needs_tessdata
def test_overlapping_regions_are_both_rejected(reader, make_reader_pdf):
    data = make_reader_pdf((upright(REPORT, 0),))
    layout = layout_for(
        [
            ("title", "text", (20, 135, 200, 160)),
            ("wide", "text", (20, 185, 290, 230)),
            ("left", "text", (20, 185, 150, 230)),
        ]
    )
    result = reader.read(data, layout=layout)
    assert result.outcome == "partial"
    assert [b.region_id for b in result.blocks] == ["title"]
    assert sorted((f.code, f.region_id) for f in result.findings) == [
        ("selection_overlap", "left"),
        ("selection_overlap", "wide"),
    ]


@needs_tessdata
def test_unusable_selections_are_reported(reader, make_reader_pdf):
    data = make_reader_pdf((upright(REPORT, 0),))
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=300,
                height=400,
            ),
            LayoutSurface(
                id_="page-5",
                location=PdfPageLocation(page_number=5),
                width=300,
                height=400,
            ),
            LayoutSurface(
                id_="body",
                kind="flow",
                location=DocxBodyLocation(part="word/document.xml"),
            ),
            LayoutSurface(
                id_="small",
                location=PdfPageLocation(page_number=1),
                width=150,
                height=200,
            ),
        ),
        regions=(
            LayoutRegion(
                id="missing", surface_id="page-5", kind="text", bbox=(0, 0, 9, 9)
            ),
            LayoutRegion(
                id="word",
                surface_id="body",
                kind="text",
                location=DocxElementLocation(
                    part="word/document.xml", child_indexes=(0,)
                ),
            ),
            LayoutRegion(
                id="mismatch", surface_id="small", kind="text", bbox=(0, 0, 9, 9)
            ),
            LayoutRegion(
                id="blank", surface_id="page-1", kind="text", bbox=(20, 300, 280, 380)
            ),
        ),
    )
    result = reader.read(data, layout=layout)
    assert result.outcome == "partial" and not result.blocks
    assert sorted((f.code, f.region_id) for f in result.findings) == [
        ("empty", "blank"),
        ("layout_mismatch", "mismatch"),
        ("page_missing", "missing"),
        ("selection_unsupported", "word"),
    ]


# Sources ---------------------------------------------------------------------------


@needs_tessdata
def test_passwords_and_non_pdf_sources(reader, make_reader_pdf):
    from raggae.documents.normalization import Artifact

    data = make_reader_pdf((text_page("Locked page"),), password="secret")
    assert reader.read(data).findings[0].code == ("password_required")
    opened = reader.read(Artifact(data=data, password="secret"))
    assert [b.text for b in opened.blocks] == ["Locked page"]
    image = BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, "PNG")
    result = reader.read(image.getvalue())
    assert result.outcome == "not_applicable"
    assert [f.code for f in result.findings] == ["not_applicable"]
