"""Native text evidence, geometric agreement, page patterns, and safe gaps."""

from raggae.documents.layout_analysis import RepeatedMarginPolicy

import builtins
import subprocess
import sys

import pytest
from pydantic import ValidationError

pytest.importorskip("pdfplumber")

from raggae.documents.layout_analysis import (
    DocxBodyLocation, LayoutAnalysisResult, LayoutFinding,
    LayoutRegion, LayoutSurface, NativePdfLayoutAnalyzer, PdfRepeatedMarginDetector,
    RepeatedMarginDetector, RepeatedMarginGroup, RepeatedMarginResult,
)
from raggae.documents.layout_analysis.map_analysis.repeated_margin.matching import signature
from raggae.documents.normalization import Artifact


def line(text, x=30, y=380, size=10):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x} {y} Td ({escaped}) Tj ET\n".encode("ascii")


def contents(number):
    return (line("Depot instructions") + line("Repeated body reminder", y=200)
            + line("Internal use", y=18) + line(f"Page {number} of 2", x=225, y=18, size=8))


@pytest.mark.parametrize("source_kind", ["bytes", "path", "string", "artifact_bytes", "artifact_path"])
@pytest.mark.parametrize("positional_source", [False, True])
def test_public_inputs_groups_reference_original_map_without_changes(make_pdf, tmp_path, source_kind, positional_source):
    data = make_pdf((contents(1), contents(2)))
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    original = layout.model_dump_json()
    path = tmp_path / "report.bin"
    path.write_bytes(data)
    source = {"bytes": data, "path": path, "string": str(path), "artifact_bytes": Artifact(data=data),
              "artifact_path": Artifact(path=path)}[source_kind]
    worker = PdfRepeatedMarginDetector()
    result = worker.detect(layout, source) if positional_source else worker.detect(layout, source=source)
    assert result.outcome == "success"
    assert sorted((group.kind, group.basis) for group in result.groups) == [
        ("footer", "page_number"), ("footer", "repeated_text"), ("header", "repeated_text"),
    ]
    indexed = {region.id: region for region in layout.regions}
    for group in result.groups:
        assert len(group.region_ids) == 2
        assert len({indexed[identifier].surface_id for identifier in group.region_ids}) == 2
        assert all(indexed[identifier].bbox[1] < 48 if group.kind == "header" else indexed[identifier].bbox[1] > 352
                   for identifier in group.region_ids)
    grouped = {identifier for group in result.groups for identifier in group.region_ids}
    assert len(grouped) == 6 and len(layout.regions) == 8
    assert "Depot instructions" not in result.model_dump_json()
    assert "Internal use" not in result.model_dump_json()
    assert RepeatedMarginResult.model_validate_json(result.model_dump_json()) == result
    assert layout.model_dump_json() == original and path.read_bytes() == data
    assert isinstance(worker, RepeatedMarginDetector) and worker.name == "layout_analysis_pdf_repeated_margin"


def test_repeated_body_text_and_different_margin_text_are_not_grouped(make_pdf):
    data = make_pdf((line("First section") + line("Same body", y=200),
                     line("Second section") + line("Same body", y=200)))
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    result = PdfRepeatedMarginDetector().detect(layout, source=data)
    assert result.outcome == "success" and not result.groups


def test_text_at_different_positions_or_opposite_margins_does_not_match(make_pdf):
    for second in (line("Same", x=200), line("Same", y=18)):
        data = make_pdf((line("Same"), second))
        layout = NativePdfLayoutAnalyzer().analyze(source=data)
        assert not PdfRepeatedMarginDetector().detect(layout, source=data).groups


def test_minimum_fraction_includes_blank_supplied_pages(make_pdf):
    data = make_pdf((line("Recurring"), line("Recurring"), b"", b""))
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    assert not PdfRepeatedMarginDetector().detect(layout, source=data).groups
    result = PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(minimum_page_fraction=0.5)).detect(layout, source=data)
    assert len(result.groups) == 1 and len(result.groups[0].region_ids) == 2


@pytest.mark.parametrize("rotation", [0, 180])
@pytest.mark.parametrize("unit", [1, 2])
def test_normalized_cropped_rotated_nonzero_origin_geometry(make_pdf, rotation, unit):
    data = make_pdf((line("Recurring"), line("Recurring")), rotation=rotation, unit=unit,
                    media=(10, 20, 310, 420), crop=(30, 20, 290, 420))
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    result = PdfRepeatedMarginDetector().detect(layout, source=data)
    assert result.outcome == "success" and len(result.groups) == 1
    assert result.groups[0].kind == ("header" if rotation == 0 else "footer")


@pytest.mark.parametrize("text, number, expected, basis", [
    ("Page 1 of 2", 1, "page # of 2", "page_number"),
    ("Internal - PAGE 2 / 2", 2, "internal - page # / 2", "page_number"),
    ("- 2 -", 2, "- # -", "page_number"),
    ("3/10", 3, "#/10", "page_number"),
    ("Revision 2026", 2, "revision 2026", "repeated_text"),
    ("Page 9", 2, "page 9", "repeated_text"),
    ("iv", 4, "iv", "repeated_text"),
    ("  INTERNAL\nUSE  ", 2, "internal use", "repeated_text"),
])
def test_signatures_do_not_blank_arbitrary_numbers(text, number, expected, basis):
    assert signature(text, number) == (expected, basis)


def test_same_page_duplicate_candidates_are_ambiguous_not_extra_support(make_pdf):
    data = make_pdf((line("Same"), line("Same")))
    original = NativePdfLayoutAnalyzer().analyze(source=data)
    duplicate = original.regions[0].model_copy(update={"id": "duplicate"})
    layout = LayoutAnalysisResult(outcome="success", surfaces=original.surfaces, regions=original.regions + (duplicate,))
    result = PdfRepeatedMarginDetector().detect(layout, source=data)
    assert result.outcome == "partial" and not result.groups
    assert result.findings[0].code == "layout.margin.ambiguous_match"


def test_images_and_upstream_coverage_are_not_claimed_as_native_text(make_pdf):
    data = make_pdf((line("Same") + b"q 20 0 0 20 200 370 cm /Im1 Do Q",) * 2, image=True, annotations=True)
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    result = PdfRepeatedMarginDetector().detect(layout, source=data)
    assert result.outcome == "partial" and len(result.groups) == 1
    assert all(finding in result.findings for finding in layout.findings)
    assert any(finding.code == "layout.margin.non_text_margin" for finding in result.findings)


def test_blank_body_only_one_page_and_non_pdf_layouts(make_pdf):
    detector = PdfRepeatedMarginDetector()
    for data in (make_pdf((b"", b"")), make_pdf((line("Body", y=200),) * 2)):
        layout = NativePdfLayoutAnalyzer().analyze(source=data)
        result = detector.detect(layout, source="not-opened.pdf")
        assert result.outcome == "success" and not result.groups
    layout = NativePdfLayoutAnalyzer().analyze(source=make_pdf((line("Header"),)))
    assert detector.detect(layout, source="not-opened.pdf").outcome == "not_applicable"
    flow = LayoutAnalysisResult(outcome="success", surfaces=(LayoutSurface(id_="body", kind="flow", location=DocxBodyLocation(part="word/document.xml")),))
    assert detector.detect(flow, source="not-opened.docx").outcome == "not_applicable"
    failed = LayoutAnalysisResult(outcome="failed", findings=(LayoutFinding(code="test.failed", message="Upstream failure."),))
    assert detector.detect(failed, source=b"").outcome == "failed"


@pytest.mark.parametrize("option, code", [("max_pages", "page_limit"), ("max_margin_regions", "candidate_limit"), ("max_objects_per_page", "object_limit")])
def test_limits_preserve_explicit_partial_results(make_pdf, option, code):
    data = make_pdf((contents(1), contents(2)))
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    result = PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(**{option: 1})).detect(layout, source=data)
    assert result.outcome == "partial"
    assert any(finding.code == "layout.margin." + code for finding in result.findings)


def test_byte_limit_and_unreadable_sources(make_pdf, tmp_path):
    data = make_pdf((line("Header"),) * 2)
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    assert PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(max_source_bytes=len(data))).detect(layout, source=data).outcome == "success"
    result = PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(max_source_bytes=len(data)-1)).detect(layout, source=data)
    assert result.outcome == "failed" and result.findings[0].code == "layout.margin.source_too_large"
    for source in (tmp_path, tmp_path / "missing.pdf", b"", b"not PDF", b"%PDF-1.7 broken"):
        result = PdfRepeatedMarginDetector().detect(layout, source=source)
        assert result.outcome == "failed" and not result.groups


def test_mismatched_dimensions_and_missing_source_page_are_rejected(make_pdf):
    data = make_pdf((line("Header"),) * 2)
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    wrong = layout.surfaces[0].model_copy(update={"width": 310})
    mismatched = LayoutAnalysisResult(outcome="success", surfaces=(wrong, layout.surfaces[1]), regions=layout.regions)
    result = PdfRepeatedMarginDetector().detect(mismatched, source=data)
    assert result.outcome == "failed" and result.findings[0].code == "layout.margin.source_mismatch"
    result = PdfRepeatedMarginDetector().detect(layout, source=make_pdf((line("Header"),)))
    assert result.outcome == "failed" and result.findings[0].code == "layout.margin.source_mismatch"


def test_password_handling_does_not_leak_secrets(make_pdf):
    data = make_pdf((line("Header"),) * 2, password="margin-fixture-password")
    layout = NativePdfLayoutAnalyzer().analyze(source=Artifact(data=data, password="margin-fixture-password"))
    for password, code in ((None, "password_required"), ("wrong-fixture-secret", "invalid_password")):
        result = PdfRepeatedMarginDetector().detect(layout, source=Artifact(data=data, password=password))
        assert result.outcome == "failed" and result.findings[0].code == "layout.margin." + code
        assert "fixture" not in result.model_dump_json()
    assert len(PdfRepeatedMarginDetector().detect(layout, source=Artifact(data=data, password="margin-fixture-password")).groups) == 1


@pytest.mark.parametrize("name", ["max_source_bytes", "max_pages", "max_objects_per_page", "max_margin_regions", "minimum_pages"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_invalid_integer_limits(name, value):
    with pytest.raises((TypeError, ValueError)):
        PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(**{name: value}))


@pytest.mark.parametrize("name", ["margin_ratio", "position_tolerance", "minimum_page_fraction"])
@pytest.mark.parametrize("value", [True, 0, float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_ratios(name, value):
    with pytest.raises((TypeError, ValueError)):
        PdfRepeatedMarginDetector(policy=RepeatedMarginPolicy(**{name: value}))


def test_public_models_are_frozen_and_reject_inconsistent_groups():
    group = RepeatedMarginGroup(kind="header", basis="repeated_text", region_ids=("r1", "r2"))
    with pytest.raises(ValidationError):
        group.kind = "footer"
    for identifiers in (("r1",), ("r1", "r1"), ("", "r2")):
        with pytest.raises(ValidationError):
            RepeatedMarginGroup(kind="header", basis="repeated_text", region_ids=identifiers)
    with pytest.raises(ValidationError):
        RepeatedMarginResult(outcome="success", groups=(group, group))
    with pytest.raises(ValidationError):
        RepeatedMarginResult(outcome="failed", groups=(group,))


def test_bad_call_styles_and_duplicate_pdf_page_locators(make_pdf):
    data = make_pdf((line("Header"),) * 2)
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    detector = PdfRepeatedMarginDetector()
    with pytest.raises(TypeError):
        detector.detect("not a map", source=data)
    with pytest.raises(TypeError):
        detector.detect(layout, data, source=data)
    duplicate = layout.surfaces[0].model_copy(update={"id_": "alias"})
    invalid = LayoutAnalysisResult(outcome="success", surfaces=layout.surfaces + (duplicate,), regions=layout.regions)
    with pytest.raises(ValueError, match="unique"):
        detector.detect(invalid, source=data)


def test_optional_import_and_missing_backend_hint(make_pdf, monkeypatch):
    script = '''
import builtins
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in ('pdfplumber', 'pdfminer'):
        raise ImportError('backend unavailable')
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from raggae.documents.layout_analysis import PdfRepeatedMarginDetector
assert PdfRepeatedMarginDetector().name == 'layout_analysis_pdf_repeated_margin'
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = make_pdf((line("Header"),) * 2)
    layout = NativePdfLayoutAnalyzer().analyze(source=data)
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name == "pdfplumber":
            raise ImportError("backend unavailable")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ImportError, match="layout-pdf"):
        PdfRepeatedMarginDetector().detect(layout, source=data)
