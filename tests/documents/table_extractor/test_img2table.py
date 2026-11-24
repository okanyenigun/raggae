"""Tables img2table finds in page images: rendered PDF pages and image files."""

import shutil
import sys
from io import BytesIO

import pytest
from PIL import Image

from raggae.documents.layout_analysis import (
    HtmlDocumentLocation,
    HtmlElementLocation,
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    NativePdfLayoutAnalyzer,
)
from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    Img2TableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout
from tests.documents.table_extractor.conftest import (
    EMPTY_GRID,
    RULED,
    RULED_GRID,
    UNRULED,
    make_pdf,
    text,
)


@pytest.fixture(autouse=True)
def backend():
    return pytest.importorskip("img2table", reason="Install raggae[table-ocr]")


class Counter:
    """Stands in for an OCR service: names each picture it is given, in order."""

    def __init__(self):
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return f" cell {self.calls} "


class Broken:
    def recognize(self, image):
        raise RuntimeError("private service details")


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def grid(table):
    return [(c.row, c.column, c.row_span, c.column_span, c.text) for c in table.cells]


def page_image(content, format="PNG", frames=1):
    """A test page rendered by PDFium and saved as an image file."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(make_pdf(content))
    try:
        picture = document[0].render(scale=300 / 72).to_pil().convert("RGB")
    finally:
        document.close()
    output = BytesIO()
    if frames > 1:
        blank = Image.new("RGB", picture.size, "white")
        blank.save(output, format, save_all=True, append_images=[picture] * (frames - 1))
    else:
        picture.save(output, format)
    return output.getvalue()


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import image

    assert image.Img2TableExtractor is Img2TableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = make_pdf(RULED)
    path = tmp_path / "prices.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(path=path),
    }[source_kind]
    extractor = Img2TableExtractor()
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_img2table"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    # img2table finds the grid and the merges in the pixels; the PDF's own text fills it.
    assert grid(table) == RULED_GRID
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "ruled",
        0,
        None,
        None,
    )
    assert not any(cell.header for cell in table.cells)
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


def test_tables_without_borders_need_the_borderless_option():
    data = make_pdf(text(30, 330, "No table here"), UNRULED)
    result = Img2TableExtractor().extract(data)
    assert not result.tables and codes(result) == [("empty", None)]
    (table,) = Img2TableExtractor(borderless=True).extract(data).tables
    assert (table.method, table.page_index) == ("aligned", 1)
    rows = {}
    for cell in table.cells:
        rows.setdefault(cell.row, []).append(cell.text)
    # The footnote under the table stays out, and there are no gap rows.
    assert list(rows.values())[:2] == [["Region", "Year", "Units"], ["North", "2025", "1200"]]
    assert len(rows) == 5


class Blank:
    """Stands in for an OCR service that recognizes nothing."""

    def recognize(self, image):
        return ""


def test_grids_without_text_are_not_returned():
    result = Img2TableExtractor(service=Blank()).extract(make_pdf(RULED))
    assert result.outcome == "success" and not result.tables
    assert codes(result) == [("empty_table", 0)]
    # img2table itself reports no table for a ruled grid with nothing inside it.
    empty = Img2TableExtractor().extract(make_pdf(EMPTY_GRID))
    assert empty.outcome == "success" and codes(empty) == [("empty", None)]


def test_an_ocr_service_reads_every_cell():
    service = Counter()
    (table,) = Img2TableExtractor(service=service).extract(make_pdf(RULED)).tables
    assert service.calls == len(table.cells) == 7
    assert [c.text for c in table.cells] == [f"cell {n}" for n in range(1, 8)]


def test_a_failing_service_leaves_cells_empty_and_says_so():
    result = Img2TableExtractor(service=Broken()).extract(make_pdf(RULED))
    assert result.outcome == "partial" and not result.tables
    assert codes(result) == [("empty_table", 0), ("ocr_failed", 0)]
    assert "private" not in result.model_dump_json()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_tesseract_reads_a_scanned_table():
    from raggae.documents.text_reader import TesseractOcrService

    extractor = Img2TableExtractor(service=TesseractOcrService(language="eng"))
    (table,) = extractor.extract(page_image(RULED)).tables
    assert grid(table) == RULED_GRID


def test_image_files_every_frame_and_a_service_required():
    service = Counter()
    result = Img2TableExtractor(service=service).extract(page_image(RULED, "TIFF", frames=2))
    assert [t.page_index for t in result.tables] == [1]
    with pytest.raises(ValueError, match="service"):
        Img2TableExtractor().extract(page_image(RULED))


@pytest.mark.parametrize("data", [b"GIF89a" + bytes(20), b"plain text, not an image"])
def test_other_sources_are_not_applicable(data):
    result = Img2TableExtractor(service=Counter()).extract(data)
    assert result.outcome == "not_applicable" and codes(result) == [("not_applicable", None)]


def test_gif_images_are_not_read():
    output = BytesIO()
    Image.new("RGB", (10, 10), "white").save(output, "GIF")
    result = Img2TableExtractor(service=Counter()).extract(output.getvalue())
    assert result.outcome == "not_applicable"


@pytest.mark.parametrize(
    ("password", "code"),
    [(None, "password_required"), ("private-wrong", "invalid_password")],
)
def test_password_errors_are_safe(password, code):
    data = make_pdf(RULED, password="private-secret")
    result = Img2TableExtractor().extract(Artifact(data=data, password=password))
    assert result.outcome == "failed" and codes(result) == [(code, None)]
    assert "private" not in result.model_dump_json()


def test_the_password_comes_from_the_artifact():
    data = make_pdf(RULED, password="table-demo-secret")
    result = Img2TableExtractor().extract(Artifact(data=data, password="table-demo-secret"))
    assert grid(result.tables[0]) == RULED_GRID


def test_empty_and_missing_sources(tmp_path):
    assert codes(Img2TableExtractor().extract(b"")) == [("decode_failed", None)]
    assert codes(Img2TableExtractor().extract(tmp_path / "missing.pdf")) == [
        ("source_unreadable", None)
    ]


@pytest.mark.parametrize(
    "options",
    [{"service": "not a service"}, {"dpi": 0}, {"dpi": True}, {"borderless": "yes"}],
)
def test_options_are_validated(options):
    with pytest.raises((TypeError, ValueError)):
        Img2TableExtractor(**options)


def test_missing_img2table_raises_an_install_instruction(monkeypatch):
    monkeypatch.setitem(sys.modules, "img2table", None)
    monkeypatch.setitem(sys.modules, "img2table.tables.image", None)
    with pytest.raises(ImportError, match=r"raggae\[table-ocr\]"):
        Img2TableExtractor().extract(make_pdf(RULED))


def test_a_detection_failure_is_reported(monkeypatch):
    from img2table.tables.image import TableImage

    def broken(self, **options):
        raise ValueError("private pixel details")

    monkeypatch.setattr(TableImage, "extract_tables", broken)
    result = Img2TableExtractor().extract(make_pdf(RULED))
    assert result.outcome == "failed" and codes(result) == [("page_unreadable", 0)]
    assert "private" not in result.model_dump_json()


# Layout regions -------------------------------------------------------------------------


def test_layout_links_each_table_to_its_table_region():
    data = make_pdf(RULED)
    layout = NativePdfLayoutAnalyzer().analyze(data)
    (region,) = [r for r in layout.regions if r.kind == "table"]
    result = Img2TableExtractor().extract(data, layout=layout)
    assert [t.region_id for t in result.tables] == [region.id]
    assert grid(result.tables[0]) == RULED_GRID
    assert result.outcome == "success" and not result.findings


def test_a_rectangle_around_a_table_without_borders():
    layout = make_layout(("units", 1, "table", (30, 45, 280, 140)))
    result = Img2TableExtractor(borderless=True).extract(make_pdf(UNRULED), layout=layout)
    (table,) = result.tables
    assert table.region_id == "units" and table.cells[0].text == "Region"


def test_overlapping_missing_and_mismatched_regions():
    data = make_pdf(RULED)
    layout = make_layout(
        ("whole", 1, "table", (20, 40, 280, 130)),
        ("same", 1, "table", (25, 45, 275, 128)),
        ("heading", 1, "text", (25, 5, 150, 30)),
        ("far", 2, "table", (20, 40, 280, 130)),
        pages=2,
    )
    result = Img2TableExtractor().extract(data, layout=layout, region_ids=["whole", "same", "heading", "far"])
    assert result.outcome == "partial" and not result.tables
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("page_missing", "far"),
        ("selection_overlap", "whole"),
        ("selection_overlap", "same"),
        ("empty", "heading"),
    ]
    larger = make_layout(("big", 1, "table", (20, 40, 280, 130)), size=(600, 800))
    assert [(f.code, f.region_id) for f in Img2TableExtractor().extract(data, layout=larger).findings] == [
        ("layout_mismatch", "big")
    ]


def test_regions_on_image_frames_and_other_surfaces():
    service = Counter()
    picture = page_image(RULED)
    width, height = Image.open(BytesIO(picture)).size
    frame = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="frame-1",
                kind="image",
                location=ImageFrameLocation(frame_number=1),
                width=width,
                height=height,
            ),
        ),
        regions=(
            LayoutRegion(id="grid", surface_id="frame-1", kind="table", bbox=(80, 170, 1160, 550)),
        ),
    )
    (table,) = Img2TableExtractor(service=service).extract(picture, layout=frame).tables
    assert table.region_id == "grid" and len(table.cells) == 7
    html = LayoutAnalysisResult(
        outcome="success",
        surfaces=(LayoutSurface(id_="html", kind="flow", location=HtmlDocumentLocation(element="document")),),
        regions=(
            LayoutRegion(
                id="markup",
                surface_id="html",
                kind="table",
                location=HtmlElementLocation(child_indexes=(0,), tag="table"),
            ),
        ),
    )
    result = Img2TableExtractor().extract(make_pdf(RULED), layout=html)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [("selection_unsupported", "markup")]


def test_empty_selections_never_open_the_source(tmp_path):
    missing = tmp_path / "missing.pdf"
    layout = make_layout(("grid", 1, "table", (20, 40, 280, 130)))
    assert codes(Img2TableExtractor().extract(missing, layout=layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
