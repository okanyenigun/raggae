"""Pictures on Excel worksheets, standalone or by layout region."""

import sys
import warnings
from io import BytesIO

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.drawing.image import Image as SheetImage

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    XlsxImageExtractor,
)
from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    XlsxDrawingLocation,
    XlsxRangeLocation,
    XlsxSheetLocation,
)
from raggae.documents.layout_analysis.region_discovery.xlsx import XlsxLayoutAnalyzer
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import WMF, make_layout, picture_file, rezip


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [(p.page_index, p.region_id, p.media_type, p.method) for p in result.images]


def save(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def extract(data, **options):
    return XlsxImageExtractor().extract(data, **options)


@pytest.fixture
def book():
    """Sheet "Products": a PNG at B2 and a JPEG at E10; sheet "Notes": a PNG at A1."""
    workbook = Workbook()
    products = workbook.active
    products.title = "Products"
    products["A1"] = "Name"
    products.add_image(SheetImage(picture_file("red")), "B2")
    products.add_image(SheetImage(picture_file("blue", "JPEG", (20, 10))), "E10")
    notes = workbook.create_sheet("Notes")
    notes.add_image(SheetImage(picture_file("green")), "A1")
    return save(workbook)


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import xlsx

    assert xlsx.XlsxImageExtractor is XlsxImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, book, source_kind):
    path = tmp_path / "book.xlsx"
    path.write_bytes(book)
    source = {
        "path": path,
        "string": str(path),
        "bytes": book,
        "artifact": Artifact(data=book, password="private-password"),
    }[source_kind]
    extractor = XlsxImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_xlsx"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, None, "image/png", "original"),
        (0, None, "image/jpeg", "original"),
        (1, None, "image/png", "original"),
    ]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == book and list(tmp_path.iterdir()) == [path]


def test_openpyxl_still_keeps_pictures_where_the_worker_reads_them():
    # The worker relies on a private openpyxl attribute; this pins it.
    sheet = Workbook().active
    sheet.add_image(SheetImage(picture_file()), "A1")
    assert len(sheet._images) == 1


# Which pictures ------------------------------------------------------------


def test_page_index_counts_chart_sheets_and_charts_are_reported(book):
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(book))
    data = workbook.create_sheet("Data", 1)
    for value in (1, 2, 3):
        data.append([value])
    chart = BarChart()
    chart.add_data(Reference(data, min_col=1, min_row=1, max_row=3))
    data.add_chart(chart, "C2")
    workbook.create_chartsheet("Chart", 2).add_chart(BarChart())
    result = extract(save(workbook))
    # Sheets: Products (0), Data (1), Chart (2), Notes (3).
    assert [p.page_index for p in result.images] == [0, 0, 3]
    assert codes(result) == [
        ("unsupported_content", 1, None),
        ("unsupported_content", 2, None),
    ]


def test_alt_text_is_the_stored_description(book):
    described = rezip(
        book,
        replace={
            "xl/drawings/drawing1.xml": lambda xml: xml.replace(
                b'descr="Picture"', b'descr="Product photo"', 1
            )
        },
    )
    assert [p.alt_text for p in extract(described).images][:2] == ["Product photo", "Picture"]


def test_pictures_openpyxl_drops_become_findings(book):
    broken = rezip(
        book, replace={"xl/media/image1.png": b"not an image", "xl/media/image3.png": WMF}
    )
    result = extract(broken)
    assert result.outcome == "partial"
    assert summary(result) == [(0, None, "image/jpeg", "original")]
    assert sorted(code for code, _, _ in codes(result)) == [
        "image_unreadable",
        "unsupported_image_type",
    ]


def test_a_dropped_drawing_is_reported_as_backend_coverage():
    message = warnings.WarningMessage(
        UserWarning("DrawingML support is incomplete and limited to charts and images only."),
        UserWarning,
        "/site-packages/openpyxl/reader/drawings.py",
        30,
    )
    unrelated = warnings.WarningMessage(
        UserWarning("Data Validation extension is not supported"),
        UserWarning,
        "/site-packages/openpyxl/worksheet/_reader.py",
        10,
    )
    [finding] = XlsxImageExtractor._load_findings([message, unrelated])
    assert finding.code == "backend_coverage"


def test_in_cell_pictures_and_empty_workbooks(book):
    in_cells = rezip(book, add={"xl/richData/rdrichvalue.xml": b"<rv/>"})
    result = extract(in_cells)
    assert result.outcome == "success" and len(result.images) == 3
    assert codes(result) == [("unsupported_content", None, None)]
    assert codes(extract(save(Workbook()))) == [("empty", None, None)]


def test_unusable_sources(tmp_path):
    from docx import Document

    document = BytesIO()
    Document().save(document)
    for data in (b"", b"not a zip file", document.getvalue()):
        result = extract(data)
        assert result.outcome == "failed" and codes(result)[0][0] == "decode_failed"
    assert codes(extract(tmp_path / "missing.xlsx")) == [("source_unreadable", None, None)]


def test_missing_openpyxl_names_the_extra(book, monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ImportError, match=r"raggae\[text-office\]"):
        extract(book)


# Layout regions ----------------------------------------------------------------


def test_layout_links_each_picture_to_its_image_region(book):
    layout = XlsxLayoutAnalyzer().analyze(book)
    images = [r.id for r in layout.regions if r.kind == "image"]
    result = extract(book, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert [(p.page_index, p.region_id) for p in result.images] == [
        (0, images[0]),
        (0, images[1]),
        (1, images[2]),
    ]


def sheet_layout(*regions):
    """Regions on the "Products" sheet: (id, location)."""
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="sheet-1",
                kind="sheet",
                location=XlsxSheetLocation(sheet_number=1, sheet_name="Products"),
            ),
        ),
        regions=tuple(
            LayoutRegion(id=identifier, surface_id="sheet-1", kind=kind, location=location)
            for identifier, kind, location in regions
        ),
    )


def drawing(index, kind="image", sheet="Products"):
    return XlsxDrawingLocation(sheet_name=sheet, drawing_kind=kind, drawing_index=index)


def cells(reference):
    return XlsxRangeLocation(sheet_name="Products", cell_range=reference)


def test_a_range_gives_the_pictures_anchored_in_it(book):
    layout = sheet_layout(
        ("top", "unknown", cells("A1:C3")),
        ("words", "text", cells("A5:A6")),
    )
    result = extract(book, layout=layout, region_ids=["top", "words"])
    assert summary(result) == [(0, "top", "image/png", "original")]
    assert codes(result) == [("empty", 0, "words")]


def test_ownership_between_drawings_and_ranges(book):
    layout = sheet_layout(
        ("photo", "image", drawing(0)),
        ("all", "unknown", cells("A1:Z99")),
        ("inner", "unknown", cells("D9:F11")),
        ("side", "unknown", cells("E1:E20")),
    )
    # The drawing region owns the PNG over the range holding it.
    result = extract(book, layout=layout, region_ids=["photo", "all"])
    assert summary(result) == [
        (0, "photo", "image/png", "original"),
        (0, "all", "image/jpeg", "original"),
    ]
    # Of nested ranges the inner one owns the JPEG at E10.
    nested = extract(book, layout=layout, region_ids=["all", "inner"])
    assert [(p.region_id, p.media_type) for p in nested.images] == [
        ("all", "image/png"),
        ("inner", "image/jpeg"),
    ]
    # Ranges that claim it without nesting are both refused.
    crossing = extract(book, layout=layout, region_ids=["inner", "side"])
    assert crossing.outcome == "failed"
    assert codes(crossing) == [
        ("selection_overlap", 0, "inner"),
        ("selection_overlap", 0, "side"),
    ]


def test_chart_locations_missing_sheets_and_out_of_range_drawings(book):
    products = sheet_layout(
        ("chart", "unknown", drawing(0, kind="chart")),
        ("far", "image", drawing(9)),
        ("photo", "image", drawing(1)),
    )
    # A valid layout region for a sheet this workbook does not have.
    archive = LayoutSurface(
        id_="sheet-9",
        kind="sheet",
        location=XlsxSheetLocation(sheet_number=9, sheet_name="Archive"),
    )
    gone = LayoutRegion(
        id="gone", surface_id="sheet-9", kind="image", location=drawing(0, sheet="Archive")
    )
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(*products.surfaces, archive),
        regions=(*products.regions, gone),
    )
    result = extract(book, layout=layout, region_ids=["chart", "gone", "far", "photo"])
    assert result.outcome == "partial"
    assert summary(result) == [(0, "photo", "image/jpeg", "original")]
    assert codes(result) == [
        ("selection_unsupported", None, "chart"),
        ("region_unresolved", None, "gone"),
        ("region_unresolved", 0, "far"),
    ]


def test_non_sheet_locations_and_empty_selections(tmp_path, book):
    pdf_layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    assert codes(extract(book, layout=pdf_layout)) == [
        ("selection_unsupported", None, "photo")
    ]
    missing = tmp_path / "missing.xlsx"
    assert codes(extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None, None)
    ]
