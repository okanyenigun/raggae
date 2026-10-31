"""Default routing, independent map workers, source preservation and overrides."""

from io import BytesIO
from itertools import product
import subprocess
import sys

from PIL import Image
import pytest

from raggae.documents.layout_analysis import (
    DocumentLayoutAnalyzer,
    DocumentLayoutReport,
    DocxBodyLocation,
    DocxElementLocation,
    HtmlDocumentLocation,
    HtmlElementLocation,
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutDetection,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
    PptxShapeLocation,
    PptxSlideLocation,
    ReadingOrderResult,
    RegionRelationshipResult,
    RepeatedMarginResult,
    XlsxRangeLocation,
    XlsxSheetLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.layout_analysis import coordinator


def mapped(kind="page", *, outcome="success"):
    surface_options = {
        "page": dict(location=PdfPageLocation(page_number=1), width=300, height=400),
        "image": dict(
            location=ImageFrameLocation(frame_number=1), width=300, height=400
        ),
        "slide": dict(
            location=PptxSlideLocation(slide_number=1, part="ppt/slides/slide1.xml"),
            width=300,
            height=400,
        ),
        "docx": dict(location=DocxBodyLocation(part="word/document.xml")),
        "html": dict(location=HtmlDocumentLocation(element="document")),
        "sheet": dict(location=XlsxSheetLocation(sheet_number=1, sheet_name="Sheet1")),
    }
    surface = LayoutSurface(
        id_="surface",
        kind="flow" if kind in ("docx", "html") else kind,
        **surface_options[kind],
    )
    regions = []
    for index, identifier in enumerate(("a", "b")):
        options = {}
        if kind in ("page", "image", "slide"):
            options["bbox"] = (10, 10 + index * 70, 100, 40 + index * 70)
        if kind == "slide":
            options["location"] = PptxShapeLocation(
                part="ppt/slides/slide1.xml", shape_id=index + 1, shape_path=(index,)
            )
        elif kind == "docx":
            options["location"] = DocxElementLocation(
                part="word/document.xml", child_indexes=(index,)
            )
        elif kind == "html":
            options["location"] = HtmlElementLocation(child_indexes=(index,), tag="p")
        elif kind == "sheet":
            options["location"] = XlsxRangeLocation(
                sheet_name="Sheet1", cell_range="A1" if index == 0 else "B1"
            )
        regions.append(
            LayoutRegion(id=identifier, surface_id="surface", kind="text", **options)
        )
    findings = (
        (LayoutFinding(code="example.coverage", message="Example discovery gap."),)
        if outcome == "partial"
        else ()
    )
    return LayoutAnalysisResult(
        outcome=outcome, surfaces=(surface,), regions=tuple(regions), findings=findings
    )


class Discovery:
    name = "example_discovery"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def analyze(self, source):
        self.calls.append(source)
        return self.result


class Ordering:
    name = "example_ordering"

    def __init__(self, result=None):
        self.result = (
            ReadingOrderResult(outcome="success") if result is None else result
        )
        self.calls = []

    def resolve_order(self, layout, *, direction="ltr"):
        self.calls.append((layout, direction))
        return self.result


class Relationships:
    name = "example_relationships"

    def __init__(self, result=None):
        self.result = (
            RegionRelationshipResult(outcome="success") if result is None else result
        )
        self.calls = []

    def detect(self, layout):
        self.calls.append(layout)
        return self.result


class Margins:
    name = "example_margins"

    def __init__(self, result=None):
        self.result = (
            RepeatedMarginResult(outcome="success") if result is None else result
        )
        self.calls = []

    def detect(self, layout, source):
        self.calls.append((layout, source))
        return self.result


@pytest.mark.parametrize(
    "extension,worker",
    [
        ("pdf", "NativePdfLayoutAnalyzer"),
        ("PDF", "NativePdfLayoutAnalyzer"),
        ("docx", "DocxLayoutAnalyzer"),
        ("pptx", "PptxLayoutAnalyzer"),
        ("xlsx", "XlsxLayoutAnalyzer"),
        ("html", "HtmlLayoutAnalyzer"),
    ],
)
def test_default_native_routing_does_not_need_to_open_source(
    monkeypatch, extension, worker
):
    discovery = Discovery(mapped())
    monkeypatch.setattr(coordinator, worker, lambda: discovery)
    result = DocumentLayoutAnalyzer().analyze(
        f"not-opened.{extension}",
        reading_order=False,
        relationships=False,
        repeated_margins=False,
    )
    assert result.analyzer == discovery.name and result.layout is discovery.result
    assert len(discovery.calls) == 1
    assert discovery.calls[0].path.name == f"not-opened.{extension}"


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "file_artifact", "memory_artifact"]
)
def test_explicit_analyzer_bypasses_routing_and_preserves_artifact_password(
    source_kind, tmp_path
):
    path = tmp_path / "source.custom"
    source = {
        "path": path,
        "string": str(path),
        "bytes": b"example",
        "file_artifact": Artifact(path=path, password="example-password"),
        "memory_artifact": Artifact(data=b"example", password="example-password"),
    }[source_kind]
    discovery, ordering, relationships, margins = (
        Discovery(mapped()),
        Ordering(),
        Relationships(),
        Margins(),
    )
    result = DocumentLayoutAnalyzer(
        analyzer=discovery,
        reading_order_resolver=ordering,
        relationship_detector=relationships,
        repeated_margin_detector=margins,
    ).analyze(source)
    normalized = discovery.calls[0]
    if isinstance(source, Artifact):
        assert normalized is source and margins.calls[0][1] is source
        assert normalized.password == "example-password"
        assert "example-password" not in result.model_dump_json()
    assert margins.calls[0][1] is normalized
    assert (
        ordering.calls[0][0]
        is relationships.calls[0]
        is margins.calls[0][0]
        is result.layout
    )


@pytest.mark.parametrize("flags", list(product((False, True), repeat=3)))
@pytest.mark.parametrize("direction", ["ltr", "rtl"])
def test_optional_jobs_all_combinations_share_the_original_map(flags, direction):
    discovery, ordering, relationships, margins = (
        Discovery(mapped()),
        Ordering(),
        Relationships(),
        Margins(),
    )
    snapshot = discovery.result.model_dump_json()
    result = DocumentLayoutAnalyzer(
        analyzer=discovery,
        reading_order_resolver=ordering,
        relationship_detector=relationships,
        repeated_margin_detector=margins,
    ).analyze(
        b"source",
        direction=direction,
        reading_order=flags[0],
        relationships=flags[1],
        repeated_margins=flags[2],
    )
    assert bool(ordering.calls) is flags[0]
    assert bool(relationships.calls) is flags[1]
    assert bool(margins.calls) is flags[2]
    assert (result.reading_order is not None) is flags[0]
    assert (result.relationships is not None) is flags[1]
    assert (result.repeated_margins is not None) is flags[2]
    if flags[0]:
        assert ordering.calls == [(discovery.result, direction)]
    assert discovery.result.model_dump_json() == snapshot


@pytest.mark.parametrize(
    "kind,ordered,pdf_margins",
    [
        ("page", True, True),
        ("image", True, False),
        ("slide", True, False),
        ("docx", False, False),
        ("html", False, False),
        ("sheet", False, False),
    ],
)
def test_applicability_is_determined_from_the_map(kind, ordered, pdf_margins):
    ordering, relationships, margins = Ordering(), Relationships(), Margins()
    result = DocumentLayoutAnalyzer(
        analyzer=Discovery(mapped(kind)),
        reading_order_resolver=ordering,
        relationship_detector=relationships,
        repeated_margin_detector=margins,
    ).analyze(b"source")
    assert bool(ordering.calls) is ordered
    assert bool(margins.calls) is pdf_margins
    assert len(relationships.calls) == 1
    assert result.outcome == "success"


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_discovery_stops_before_any_map_worker(outcome):
    layout = LayoutAnalysisResult(
        outcome=outcome,
        findings=(LayoutFinding(code="example.unusable", message="No usable map."),),
    )
    ordering, relationships, margins = Ordering(), Relationships(), Margins()
    result = DocumentLayoutAnalyzer(
        analyzer=Discovery(layout),
        reading_order_resolver=ordering,
        relationship_detector=relationships,
        repeated_margin_detector=margins,
    ).analyze(b"source")
    assert result.outcome == outcome and result.layout is layout
    assert (
        result.reading_order is result.relationships is result.repeated_margins is None
    )
    assert not ordering.calls and not relationships.calls and not margins.calls


def test_failed_refinement_preserves_discovery_and_other_independent_results():
    gap = LayoutFinding(code="example.order_failed", message="Order analysis failed.")
    ordering = Ordering(ReadingOrderResult(outcome="failed", findings=(gap,)))
    relationships, margins = Relationships(), Margins()
    layout = mapped(outcome="partial")
    result = DocumentLayoutAnalyzer(
        analyzer=Discovery(layout),
        reading_order_resolver=ordering,
        relationship_detector=relationships,
        repeated_margin_detector=margins,
    ).analyze(b"source")
    assert result.outcome == "partial" and result.layout is layout
    assert result.reading_order.outcome == "failed"
    assert result.relationships is relationships.result
    assert result.repeated_margins is margins.result
    assert result.findings == layout.findings + (gap,)


@pytest.mark.parametrize(
    "extension", ["txt", "csv", "md", "json", "zip", "htm", "avif"]
)
def test_unsupported_extensions_do_not_read_files_or_run_jobs(extension):
    result = DocumentLayoutAnalyzer().analyze(f"does-not-exist.{extension}")
    assert result.outcome == "not_applicable" and result.analyzer is None
    assert result.findings[0].code == "layout.coordinator.unsupported_format"
    assert (
        result.reading_order is result.relationships is result.repeated_margins is None
    )


@pytest.mark.parametrize(
    "extension", ["png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp", "gif"]
)
def test_images_require_an_explicit_service_or_analyzer(extension):
    result = DocumentLayoutAnalyzer().analyze(f"not-opened.{extension}")
    assert result.outcome == "failed" and result.analyzer is None
    assert result.findings[0].code == "layout.coordinator.visual_service_required"


@pytest.mark.parametrize(
    "source", [b"source", Artifact(data=b"source"), "extensionless"]
)
def test_missing_routing_format_is_a_clear_caller_error(source):
    with pytest.raises(ValueError, match="source_format"):
        DocumentLayoutAnalyzer().analyze(source)


@pytest.mark.parametrize("format_", ["pdf", ".PDF", "  PDF  "])
def test_explicit_format_supports_bytes_and_overrides_filename(monkeypatch, format_):
    discovery = Discovery(mapped())
    monkeypatch.setattr(coordinator, "NativePdfLayoutAnalyzer", lambda: discovery)
    result = DocumentLayoutAnalyzer().analyze("misleading.docx", source_format=format_)
    assert result.layout is discovery.result
    result = DocumentLayoutAnalyzer().analyze(b"bytes", source_format=format_)
    assert result.layout is discovery.result


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"source_format": 1}, TypeError),
        ({"source_format": ""}, ValueError),
        ({"direction": "invalid"}, ValueError),
        ({"reading_order": 1}, TypeError),
        ({"relationships": "yes"}, TypeError),
        ({"repeated_margins": None}, TypeError),
    ],
)
def test_bad_public_options_fail_before_file_access(kwargs, error):
    with pytest.raises(error):
        DocumentLayoutAnalyzer().analyze("not-opened.pdf", **kwargs)


@pytest.mark.parametrize("source", [None, 123, True, bytearray(b"source"), object()])
def test_bad_source_types_remain_caller_errors(source):
    with pytest.raises(TypeError, match="source"):
        DocumentLayoutAnalyzer().analyze(source)


def test_analyzer_and_routed_visual_service_are_not_ambiguous():
    with pytest.raises(ValueError, match="not both"):
        DocumentLayoutAnalyzer(analyzer=Discovery(mapped()), visual_service=object())


def test_missing_dependency_and_unexpected_worker_errors_are_not_hidden():
    class BrokenDiscovery:
        name = "broken"

        def analyze(self, source):
            raise ImportError("example optional dependency")

    with pytest.raises(ImportError, match="optional"):
        DocumentLayoutAnalyzer(analyzer=BrokenDiscovery()).analyze(b"source")

    class BrokenOrdering:
        name = "broken_ordering"

        def resolve_order(self, layout, *, direction="ltr"):
            raise RuntimeError("example programming error")

    with pytest.raises(RuntimeError, match="programming"):
        DocumentLayoutAnalyzer(
            analyzer=Discovery(mapped()), reading_order_resolver=BrokenOrdering()
        ).analyze(b"source")


def test_real_pdf_default_flow_and_repeated_headers(make_pdf, tmp_path):
    pytest.importorskip("pdfplumber")
    data = make_pdf(
        (
            b"BT /F1 12 Tf 30 380 Td (Shared report header) Tj ET BT /F1 12 Tf 30 220 Td (First body) Tj ET",
            b"BT /F1 12 Tf 30 380 Td (Shared report header) Tj ET BT /F1 12 Tf 30 220 Td (Second body) Tj ET",
        )
    )
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    result = DocumentLayoutAnalyzer().analyze(path)
    assert result.analyzer == "layout_analysis_native_pdf"
    assert result.outcome == "success"
    assert len(result.layout.surfaces) == 2
    assert result.reading_order is not None and result.relationships is not None
    assert result.repeated_margins is not None and result.repeated_margins.groups
    assert path.read_bytes() == data
    assert DocumentLayoutReport.model_validate_json(result.model_dump_json()) == result


def test_real_html_skips_geometry_jobs_but_keeps_structural_relationships():
    pytest.importorskip("bs4")
    result = DocumentLayoutAnalyzer().analyze(
        b'<figure><img src="not-fetched.png"><figcaption>Example caption.</figcaption></figure>',
        source_format="html",
    )
    assert result.outcome == "success"
    assert result.reading_order is result.repeated_margins is None
    assert result.relationships.relationships[0].basis == "shared_parent"


def test_real_image_flow_uses_injected_service_without_loading_a_model():
    class Service:
        def __init__(self):
            self.inputs = []

        def detect(self, image):
            self.inputs.append((image.mode, image.size))
            return (
                LayoutDetection(kind="text", bbox=(10, 5, 70, 20)),
                LayoutDetection(kind="text", bbox=(10, 30, 70, 45)),
            )

    service = Service()
    with BytesIO() as stream, Image.new("RGB", (80, 60), "white") as image:
        image.save(stream, format="PNG")
        source = stream.getvalue()
    result = DocumentLayoutAnalyzer(visual_service=service).analyze(
        source, source_format="png"
    )
    assert service.inputs == [("RGB", (80, 60))]
    assert result.outcome == "success" and result.analyzer == "layout_analysis_visual"
    assert result.layout.surfaces[0].unit == "px"
    assert result.reading_order is not None and result.relationships is not None
    assert result.repeated_margins is None


def test_configured_visual_service_is_not_an_automatic_pdf_fallback():
    pytest.importorskip("pdfplumber")

    class UnusedService:
        def detect(self, image):
            raise AssertionError("Native PDF failure must not invoke a visual model")

    result = DocumentLayoutAnalyzer(visual_service=UnusedService()).analyze(
        b"not a PDF", source_format="pdf"
    )
    assert (
        result.outcome == "not_applicable"
        and result.analyzer == "layout_analysis_native_pdf"
    )


def test_coordinator_import_does_not_load_optional_runtimes():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from raggae.documents.layout_analysis import DocumentLayoutAnalyzer, DocumentLayoutReport
DocumentLayoutAnalyzer()
assert not any(name in sys.modules for name in (
    'pdfplumber', 'docx', 'pptx', 'openpyxl', 'bs4',
    'pypdfium2', 'paddleocr', 'paddle', 'torch', 'transformers',
))
""",
        ],
        check=True,
    )
