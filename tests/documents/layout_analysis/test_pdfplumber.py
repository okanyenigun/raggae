"""Real backend tests, including source locators that a reader can use."""

from raggae.documents.layout_analysis import PdfLayoutPolicy

import builtins
from collections import Counter
from io import BytesIO
import os
import subprocess
import sys

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult, LayoutAnalyzer, NativePdfLayoutAnalyzer,
)
from raggae.documents.normalization import Artifact, PdfDecryptionNormalizer
from raggae.documents.validation import DocumentInspector


@pytest.fixture
def pdfplumber():
    return pytest.importorskip("pdfplumber", reason="Install raggae[layout-pdf] for backend tests")


def test_public_import_does_not_require_optional_backend():
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('pdfplumber', 'pdfminer'):
        raise ImportError('backend deliberately unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer, LayoutAnalyzer
assert isinstance(NativePdfLayoutAnalyzer(), LayoutAnalyzer)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_kind", ["bytes", "path", "string", "artifact_bytes", "artifact_path"])
@pytest.mark.parametrize("positional_source", [False, True])
def test_sources_and_call_styles_preserve_file(
    pdfplumber, invoice_pdf, tmp_path, source_kind, positional_source,
):
    path = tmp_path / "invoice.bin"  # Extension does not choose the explicit worker.
    path.write_bytes(invoice_pdf)
    sources = {
        "bytes": invoice_pdf, "path": path, "string": str(path),
        "artifact_bytes": Artifact(data=invoice_pdf), "artifact_path": Artifact(path=path),
    }
    worker = NativePdfLayoutAnalyzer()
    source = sources[source_kind]
    result = worker.analyze(source) if positional_source else worker.analyze(source=source)
    assert isinstance(worker, LayoutAnalyzer)
    assert worker.name == "layout_analysis_native_pdf"
    assert result.outcome == "success"
    assert Counter(region.kind for region in result.regions) == {"text": 3, "table": 1, "image": 1}
    assert path.read_bytes() == invoice_pdf
    assert list(tmp_path.iterdir()) == [path]
    assert all(region.native_order is None for region in result.regions)
    assert "Report overview" not in result.model_dump_json()
    assert LayoutAnalysisResult.model_validate_json(result.model_dump_json()) == result


def test_locators_allow_separate_column_and_table_extraction(pdfplumber, invoice_pdf):
    result = NativePdfLayoutAnalyzer().analyze(source=invoice_pdf)
    with pdfplumber.open(BytesIO(invoice_pdf)) as document:
        page = document.pages[result.surfaces[0].location.page_number - 1]
        texts = [page.crop(region.bbox).extract_text() for region in result.regions if region.kind == "text"]
        assert set(texts) == {"Report overview", "Left one\nLeft two", "Right one\nRight two"}
        table, = [region for region in result.regions if region.kind == "table"]
        assert table.bbox == pytest.approx((40, 120, 240, 200))
        assert page.crop(table.bbox).extract_table() == [["A", "B"], ["C", "D"]]


@pytest.mark.parametrize("rotation,expected,size", [
    (0, (60, 230, 100, 260), (240, 320)),
    (90, (60, 60, 90, 100), (320, 240)),
    (180, (140, 60, 180, 90), (240, 320)),
    (270, (230, 140, 260, 180), (320, 240)),
])
@pytest.mark.parametrize("origin", [(0, 0), (10, 20)])
@pytest.mark.parametrize("unit", [1, 2])
def test_crop_rotation_media_origin_and_unit(
    pdfplumber, make_pdf, rotation, expected, size, origin, unit,
):
    dx, dy = origin
    content = f"q 40 0 0 30 {80 + dx} {90 + dy} cm /Im1 Do Q".encode()
    source = make_pdf(
        (content,), image=True, rotation=rotation, unit=unit,
        media=(dx, dy, 300 + dx, 400 + dy), crop=(20 + dx, 30 + dy, 260 + dx, 350 + dy),
    )
    result = NativePdfLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "partial"  # Its pixels are deliberately not classified.
    image, = result.regions
    assert image.kind == "image"
    assert image.bbox == pytest.approx(tuple(value * unit for value in expected))
    surface, = result.surfaces
    assert (surface.width, surface.height) == pytest.approx(tuple(value * unit for value in size))
    assert surface.unit == "pt"


def test_blank_and_scan_are_not_confused(pdfplumber, make_pdf):
    source = make_pdf((b"", b"q 300 0 0 400 0 0 cm /Im1 Do Q"), image=True)
    result = NativePdfLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "partial"
    assert len(result.surfaces) == 2
    image, = result.regions
    assert image.surface_id == "page-2"
    assert result.findings[0].code == "layout.pdf.image_content_unanalyzed"
    assert result.findings[0].surface_id == "page-2"
    blank = NativePdfLayoutAnalyzer().analyze(source=make_pdf())
    assert blank.outcome == "success" and blank.regions == () and blank.findings == ()


def test_annotations_and_vector_graphics_report_coverage_gaps(pdfplumber, make_pdf):
    result = NativePdfLayoutAnalyzer().analyze(
        source=make_pdf((b"10 20 30 40 re f",), annotations=True),
    )
    assert result.outcome == "partial"
    assert {finding.code for finding in result.findings} == {
        "layout.pdf.annotations_unanalyzed", "layout.pdf.vector_content_unclassified",
    }


@pytest.mark.parametrize("password,code", [(None, "password_required"), ("wrong-example", "invalid_password")])
def test_password_failures_do_not_leak_input(pdfplumber, make_pdf, password, code):
    source = make_pdf(password="layout-fixture-password")  # ggignore: synthetic fixture password.
    result = NativePdfLayoutAnalyzer().analyze(source=Artifact(data=source, password=password))
    assert result.outcome == "failed"
    assert result.findings[0].code == f"layout.pdf.{code}"
    assert result.surfaces == result.regions == ()
    assert "layout-fixture-password" not in repr(result)
    assert "wrong-example" not in result.model_dump_json()


def test_password_and_normalized_artifact_both_work(pdfplumber, make_pdf, tmp_path):
    password = "layout-fixture-password"  # ggignore: synthetic fixture password.
    source = make_pdf((b"BT /F1 12 Tf 30 300 Td (Hello) Tj ET",), password=password)
    path = tmp_path / "protected.pdf"
    path.write_bytes(source)
    protected = Artifact(path=path, password=password)
    inspector = DocumentInspector()
    assert inspector.validate(protected) == inspector.validate(path, password=password)
    worker = NativePdfLayoutAnalyzer()
    direct = worker.analyze(source=protected)
    normalized = PdfDecryptionNormalizer().normalize(source=protected, output_path=None)
    assert normalized.outcome == "changed"
    assert normalized.artifact.password is None
    assert protected.password == password
    assert path.read_bytes() == source
    assert worker.analyze(source=normalized.artifact) == direct
    assert direct.outcome == "success" and len(direct.regions) == 1


@pytest.mark.parametrize("data,outcome", [
    (b"", "failed"), (b"%PDF-1.7\ninvalid", "failed"),
    (b"<html>Hello</html>", "not_applicable"),
])
def test_empty_malformed_and_wrong_format(pdfplumber, data, outcome):
    result = NativePdfLayoutAnalyzer().analyze(source=data)
    assert result.outcome == outcome
    assert result.findings and not result.surfaces and not result.regions


def test_missing_file_directory_and_pipe_are_not_opened(tmp_path):
    worker = NativePdfLayoutAnalyzer()
    for path in (tmp_path / "missing.pdf", tmp_path):
        assert worker.analyze(source=path).findings[0].code == "layout.pdf.source_unreadable"
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe.pdf"
        os.mkfifo(fifo)
        assert worker.analyze(source=fifo).findings[0].code == "layout.pdf.source_unreadable"


def test_limits_are_enforced_and_explained(pdfplumber, make_pdf, tmp_path, monkeypatch):
    from pdfplumber.page import Page

    data = make_pdf((b"", b""))
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    small = NativePdfLayoutAnalyzer(policy=PdfLayoutPolicy(max_source_bytes=len(data) - 1))
    assert small.analyze(source=data).findings[0].code == "layout.pdf.source_too_large"
    assert small.analyze(source=path).findings[0].code == "layout.pdf.source_too_large"
    assert NativePdfLayoutAnalyzer(policy=PdfLayoutPolicy(max_source_bytes=len(data))).analyze(source=data).outcome == "success"
    original = Page.__init__
    def only_first(self, pdf, page_obj, page_number, *args, **kwargs):
        assert page_number == 1, "page limit must prevent constructing later pages, including on cleanup"
        return original(self, pdf, page_obj, page_number, *args, **kwargs)
    monkeypatch.setattr(Page, "__init__", only_first)
    limited = NativePdfLayoutAnalyzer(policy=PdfLayoutPolicy(max_pages=1)).analyze(source=data)
    assert limited.outcome == "partial" and len(limited.surfaces) == 1
    assert limited.findings[0].code == "layout.pdf.page_limit"
    dense = make_pdf((b"BT /F1 12 Tf 30 300 Td (Too many objects) Tj ET",))
    result = NativePdfLayoutAnalyzer(policy=PdfLayoutPolicy(max_objects_per_page=2)).analyze(source=dense)
    assert result.outcome == "partial" and not result.regions
    assert result.findings[0].code == "layout.pdf.object_limit"


def test_page_failure_preserves_other_pages_and_hides_diagnostics(pdfplumber, make_pdf, monkeypatch):
    from pdfplumber.page import Page
    from pdfplumber.utils.exceptions import PdfminerException

    original = Page.parse_objects
    def broken_page(self):
        if self.page_number == 2:
            raise PdfminerException("private document content")
        return original(self)
    monkeypatch.setattr(Page, "parse_objects", broken_page)
    result = NativePdfLayoutAnalyzer().analyze(source=make_pdf((b"", b"", b"")))
    assert result.outcome == "partial"
    assert [surface.location.page_number for surface in result.surfaces] == [1, 3]
    assert result.findings[0].code == "layout.pdf.page_unreadable"
    assert "private" not in result.model_dump_json()


def test_missing_backend_has_install_instruction(make_pdf, monkeypatch):
    original = builtins.__import__
    def without_backend(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("deliberately unavailable")
        return original(name, *args, **kwargs)
    data = make_pdf()
    monkeypatch.setattr(builtins, "__import__", without_backend)
    with pytest.raises(ImportError, match=r"raggae\[layout-pdf\]"):
        NativePdfLayoutAnalyzer().analyze(source=data)


def test_bad_call_styles_fail_before_reading():
    worker = NativePdfLayoutAnalyzer()
    source = "missing.pdf"
    for call in (
        lambda: worker.analyze(), lambda: worker.analyze({}),
        lambda: worker.analyze(source, source=b"pdf"),
        lambda: worker.analyze(source=b"pdf", output_path="unused.pdf"),
    ):
        with pytest.raises(TypeError):
            call()


@pytest.mark.parametrize("field", ["max_source_bytes", "max_pages", "max_objects_per_page"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_bad_limits_are_caller_errors(field, value):
    with pytest.raises((TypeError, ValueError)):
        NativePdfLayoutAnalyzer(policy=PdfLayoutPolicy(**{field: value}))
