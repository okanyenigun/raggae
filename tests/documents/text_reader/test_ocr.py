"""OcrTextReader mechanics with a stand-in service, and the real Tesseract service."""

import builtins
import shutil
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

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
    ImageFrameLocation,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    OcrService,
    OcrTextReader,
    TesseractOcrService,
    TextReader,
    TextReadResult,
)

REPORT = (
    b"BT /F1 14 Tf 30 380 Td (Purchase report) Tj ET\n"
    b"BT /F1 12 Tf 30 330 Td (Shipping labels) Tj 0 -14 Td (Cost: 90 USD) Tj ET\n"
    b"BT /F1 12 Tf 180 330 Td (Packing tape) Tj 0 -14 Td (Cost: 18 USD) Tj ET"
)


class Recorder:
    """A stand-in OCR service: records image sizes and returns scripted text."""

    def __init__(self, texts=None, fail_on=None):
        self.sizes, self.texts, self.fail_on = [], list(texts or []), fail_on

    def recognize(self, image):
        assert image.mode == "RGB"
        self.sizes.append(image.size)
        if self.fail_on is not None and len(self.sizes) == self.fail_on:
            raise RuntimeError("private engine detail")
        return self.texts.pop(0) if self.texts else f"text {len(self.sizes)}"


def picture(text, size=(900, 200)):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text(
        (30, 60), text, fill="black", font=ImageFont.load_default(size=48)
    )
    return image


def png(image):
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def tiff(*frames):
    output = BytesIO()
    frames[0].save(output, "TIFF", save_all=True, append_images=list(frames[1:]))
    return output.getvalue()


def pdf_layout(*regions, width=300, height=400, page=1):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page",
                location=PdfPageLocation(page_number=page),
                width=width,
                height=height,
            ),
        ),
        regions=tuple(
            LayoutRegion(id=name, surface_id="page", kind="text", bbox=box)
            for name, box in regions
        ),
    )


def image_layout(*regions, width=900, height=200, frame=1):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="frame",
                kind="image",
                location=ImageFrameLocation(frame_number=frame),
                width=width,
                height=height,
            ),
        ),
        regions=tuple(
            LayoutRegion(id=name, surface_id="frame", kind="text", bbox=box)
            for name, box in regions
        ),
    )


def codes(result):
    return [(f.code, f.region_id, f.page_index) for f in result.findings]


# Reader mechanics (stand-in service) ---------------------------------------


def test_family_exports():
    from raggae.documents.text_reader import ocr

    assert ocr.OcrTextReader is OcrTextReader and ocr.OcrService is OcrService
    assert ocr.TesseractOcrService is TesseractOcrService
    assert isinstance(Recorder(), OcrService)


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, make_reader_pdf, source_kind
):
    data = make_reader_pdf((REPORT, b"BT /F1 12 Tf 30 330 Td (Two) Tj ET"))
    path = tmp_path / "scan.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path),
        "artifact_bytes": Artifact(data=data),
    }[source_kind]
    reader = OcrTextReader(service=Recorder(["Page one", "Page two"]))
    assert isinstance(reader, TextReader) and reader.name == "text_reader_ocr"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, b.text, b.method) for b in result.blocks] == [
        (0, "Page one", "ocr"),
        (1, "Page two", "ocr"),
    ]
    assert all(
        b.region_id is None and b.content_format == "plain_text" for b in result.blocks
    )
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data


@pytest.mark.parametrize(
    "dpi,expected", [(72, (300, 400)), (150, (625, 833)), (300, (1250, 1667))]
)
def test_pdf_pages_render_at_the_configured_dpi(make_reader_pdf, dpi, expected):
    service = Recorder()
    OcrTextReader(service=service, dpi=dpi).read(make_reader_pdf((REPORT,)))
    width, height = service.sizes[0]
    assert abs(width - expected[0]) <= 1 and abs(height - expected[1]) <= 1


@pytest.mark.parametrize("rotation", [90, 270])
def test_pdf_pages_render_as_displayed(make_reader_pdf, rotation):
    service = Recorder()
    OcrTextReader(service=service, dpi=72).read(
        make_reader_pdf((REPORT,), rotation=rotation)
    )
    assert service.sizes == [(400, 300)]


def test_userunit_scales_rendering(make_reader_pdf):
    service = Recorder()
    OcrTextReader(service=service, dpi=72).read(make_reader_pdf((REPORT,), unit=2))
    assert service.sizes == [(600, 800)]


def test_regions_are_cropped_from_one_render_per_page(make_reader_pdf):
    service = Recorder(["Left", "Right"])
    layout = pdf_layout(("left", (20, 50, 150, 100)), ("right", (170, 50, 280, 100)))
    result = OcrTextReader(service=service, dpi=144).read(
        make_reader_pdf((REPORT,)), layout=layout
    )
    assert result.outcome == "success"
    assert [(b.region_id, b.text) for b in result.blocks] == [
        ("left", "Left"),
        ("right", "Right"),
    ]
    assert service.sizes == [(260, 100), (220, 100)]


def test_overlapping_regions_are_both_rejected(make_reader_pdf):
    service = Recorder(["Other"])
    layout = pdf_layout(
        ("a", (20, 50, 150, 100)),
        ("b", (100, 60, 200, 120)),
        ("c", (20, 200, 100, 250)),
    )
    result = OcrTextReader(service=service).read(
        make_reader_pdf((REPORT,)), layout=layout
    )
    assert result.outcome == "partial"
    assert [b.region_id for b in result.blocks] == ["c"]
    assert codes(result) == [
        ("selection_overlap", "a", 0),
        ("selection_overlap", "b", 0),
    ]
    assert len(service.sizes) == 1


def test_touching_regions_do_not_overlap(make_reader_pdf):
    layout = pdf_layout(("a", (0, 0, 100, 100)), ("b", (100, 0, 200, 100)))
    result = OcrTextReader(service=Recorder(), dpi=72).read(
        make_reader_pdf((REPORT,)), layout=layout
    )
    assert result.outcome == "success" and len(result.blocks) == 2


def test_layout_mismatch_missing_page_and_unsupported_surfaces(make_reader_pdf):
    data = make_reader_pdf((REPORT,))
    wrong_size = pdf_layout(("a", (0, 0, 10, 10)), width=600)
    assert codes(OcrTextReader(service=Recorder()).read(data, layout=wrong_size)) == [
        ("layout_mismatch", "a", 0)
    ]
    missing = pdf_layout(("a", (0, 0, 10, 10)), page=9)
    assert codes(OcrTextReader(service=Recorder()).read(data, layout=missing)) == [
        ("page_missing", "a", 8)
    ]
    flow = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="body",
                kind="flow",
                location=DocxBodyLocation(part="word/document.xml"),
            ),
        ),
        regions=(
            LayoutRegion(
                id="docx",
                surface_id="body",
                kind="text",
                location=__import__(
                    "raggae.documents.layout_analysis.models",
                    fromlist=["DocxElementLocation"],
                ).DocxElementLocation(part="word/document.xml", child_indexes=(0,)),
            ),
        ),
    )
    assert codes(OcrTextReader(service=Recorder()).read(data, layout=flow)) == [
        ("selection_unsupported", "docx", None)
    ]


def test_image_regions_must_be_on_image_frames(make_reader_pdf):
    layout = image_layout(("a", (0, 0, 10, 10)))
    result = OcrTextReader(service=Recorder()).read(
        make_reader_pdf((REPORT,)), layout=layout
    )
    assert codes(result) == [("selection_unsupported", "a", None)]


def test_images_and_every_tiff_frame():
    service = Recorder(["Receipt"])
    assert [
        b.text for b in OcrTextReader(service=service).read(png(picture("x"))).blocks
    ] == ["Receipt"]
    frames = OcrTextReader(service=Recorder(["One", "Two"])).read(
        tiff(picture("1"), picture("2"))
    )
    assert [(b.page_index, b.text) for b in frames.blocks] == [(0, "One"), (1, "Two")]


def test_image_frame_regions_crop_decoded_pixels():
    service = Recorder(["Crop"])
    data = tiff(picture("1"), picture("2"))
    layout = image_layout(("r", (10, 20, 110, 70)), frame=2)
    result = OcrTextReader(service=service).read(data, layout=layout)
    assert [(b.region_id, b.page_index, b.text) for b in result.blocks] == [
        ("r", 1, "Crop")
    ]
    assert service.sizes == [(100, 50)]


def test_transparent_images_are_composited_on_white():
    seen = []

    class Inspect:
        def recognize(self, image):
            seen.append(image.getpixel((0, 0)))
            return "x"

    transparent = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    OcrTextReader(service=Inspect()).read(png(transparent))
    assert seen == [(255, 255, 255)]


def test_empty_recognition_and_whitespace_are_reported(make_reader_pdf):
    result = OcrTextReader(service=Recorder(["  \n\x0c", "Text"])).read(
        make_reader_pdf((b"", REPORT))
    )
    assert result.outcome == "success"
    assert [(b.page_index, b.text) for b in result.blocks] == [(1, "Text")]
    assert codes(result) == [("empty", None, 0)]


def test_service_failures_are_reported_per_page_and_others_kept(make_reader_pdf):
    result = OcrTextReader(service=Recorder(["One", "Three"], fail_on=2)).read(
        make_reader_pdf((REPORT, REPORT, REPORT))
    )
    assert result.outcome == "partial"
    assert [(b.page_index, b.text) for b in result.blocks] == [(0, "One"), (2, "Three")]
    assert codes(result) == [("ocr_failed", None, 1)]
    assert "private" not in result.model_dump_json()


def test_all_failures_fail(make_reader_pdf):
    result = OcrTextReader(service=Recorder(fail_on=1)).read(make_reader_pdf((REPORT,)))
    assert result.outcome == "failed" and not result.blocks


def test_passwords_are_applied_and_never_revealed(make_reader_pdf):
    data = make_reader_pdf((REPORT,), password="private-pass")
    opened = OcrTextReader(service=Recorder(["Secret"])).read(
        Artifact(data=data, password="private-pass")
    )
    assert [b.text for b in opened.blocks] == ["Secret"]
    for given, code in (
        (None, "password_required"),
        ("wrong-private", "invalid_password"),
    ):
        result = OcrTextReader(service=Recorder()).read(
            Artifact(data=data, password=given)
        )
        assert codes(result) == [(code, None, None)]
        assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"%PDF-1.7\nprivate garbage", "failed", "decode_failed"),
        (b"private plain text", "not_applicable", "not_applicable"),
    ],
)
def test_empty_broken_and_unsupported_sources(data, outcome, code):
    service = Recorder()
    result = OcrTextReader(service=service).read(data)
    assert result.outcome == outcome and not result.blocks
    assert codes(result) == [(code, None, None)]
    assert service.sizes == []
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files(tmp_path, monkeypatch):
    reader = OcrTextReader(service=Recorder())
    result = reader.read(tmp_path / "private-name.pdf")
    assert codes(result) == [("source_unreadable", None, None)]
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


@pytest.mark.parametrize("dpi", [0, -1, float("nan"), float("inf"), "300", True, None])
def test_dpi_is_validated(dpi):
    with pytest.raises(ValueError):
        OcrTextReader(service=Recorder(), dpi=dpi)


@pytest.mark.parametrize("service", [None, object(), "tesseract"])
def test_service_must_recognize(service):
    with pytest.raises(TypeError, match="recognize"):
        OcrTextReader(service=service)


def test_missing_pdf_renderer_has_an_install_hint(monkeypatch, make_reader_pdf):
    real_import = builtins.__import__

    def without_pdfium(name, *args, **kwargs):
        if name == "pypdfium2" or name.startswith("pypdfium2."):
            raise ImportError("no pypdfium2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pdfium)
    with pytest.raises(ImportError, match=r"raggae\[image-pdf\]"):
        OcrTextReader(service=Recorder()).read(make_reader_pdf())


def test_empty_selection_reads_nothing_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    result = OcrTextReader(service=Recorder()).read(
        tmp_path / "x.pdf", layout=pdf_layout(("a", (0, 0, 1, 1))), region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_streams_are_not_silently_added_to_source_contract(make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        OcrTextReader(service=Recorder()).read(BytesIO(make_reader_pdf()))


# Real Tesseract ---------------------------------------------------------------

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="Tesseract engine not installed"
)


@pytest.fixture(scope="module")
def tesseract():
    pytest.importorskip("pytesseract", reason="Install raggae[text-tesseract]")
    if shutil.which("tesseract") is None:
        pytest.skip("Tesseract engine not installed")
    try:
        return TesseractOcrService(language="eng")
    except ValueError:
        pytest.skip("Tesseract English language data not installed")


@needs_tesseract
def test_real_tesseract_reads_pages_regions_images_and_image_only_pdfs(
    tesseract, make_reader_pdf
):
    reader = OcrTextReader(service=tesseract)
    data = make_reader_pdf((REPORT,))
    layout = NativePdfLayoutAnalyzer().analyze(data)
    regions = reader.read(data, layout=layout)
    assert [b.text for b in regions.blocks] == [
        "Purchase report",
        "Shipping labels\nCost: 90 USD",
        "Packing tape\nCost: 18 USD",
    ]
    assert [b.text for b in reader.read(png(picture("Scanned receipt 42"))).blocks] == [
        "Scanned receipt 42"
    ]
    image_pdf = BytesIO()
    picture("Image only page").save(image_pdf, "PDF", resolution=150)
    assert [b.text for b in reader.read(image_pdf.getvalue()).blocks] == [
        "Image only page"
    ]


@needs_tesseract
def test_tesseract_service_checks_languages_and_binary(tesseract, tmp_path):
    assert tesseract.language == "eng"
    with pytest.raises(ValueError, match="'zzz' is not installed"):
        TesseractOcrService(language="eng+zzz")
    with pytest.raises(FileNotFoundError, match="Tesseract engine was not found"):
        TesseractOcrService(language="eng", executable=tmp_path / "no-tesseract")


@needs_tesseract
def test_tesseract_does_not_leak_its_binary_setting(tesseract):
    import pytesseract

    before = pytesseract.pytesseract.tesseract_cmd
    tesseract.recognize(picture("x"))
    assert pytesseract.pytesseract.tesseract_cmd == before


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"language": None}, TypeError),
        ({"language": ""}, ValueError),
        ({"language": "eng; rm"}, ValueError),
        ({"language": "eng+"}, ValueError),
        ({"language": "eng", "timeout": 0}, ValueError),
        ({"language": "eng", "timeout": -1}, ValueError),
        ({"language": "eng", "timeout": True}, ValueError),
        ({"language": "eng", "executable": 3}, TypeError),
    ],
)
def test_tesseract_configuration_is_validated(kwargs, error):
    with pytest.raises(error):
        TesseractOcrService(**kwargs)


def test_tesseract_requires_the_language_argument():
    with pytest.raises(TypeError):
        TesseractOcrService()  # type: ignore[call-arg]


def test_missing_pytesseract_has_an_install_hint(monkeypatch):
    real_import = builtins.__import__

    def without_pytesseract(name, *args, **kwargs):
        if name == "pytesseract" or name.startswith("pytesseract."):
            raise ImportError("no pytesseract")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pytesseract)
    with pytest.raises(ImportError, match=r"raggae\[text-tesseract\]"):
        TesseractOcrService(language="eng")
