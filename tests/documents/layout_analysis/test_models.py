"""Public input boundaries and usable, immutable layout results."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult, LayoutFinding, LayoutRegion, NativePdfLayoutAnalyzer,
    LayoutSurface, PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.layout_analysis.utils import resolve_source


def test_source_resolution_preserves_artifact_and_hides_password():
    artifact = Artifact(data=b"private document", password="private password")
    assert resolve_source(artifact) is artifact
    assert artifact.password == "private password"
    assert "private" not in repr(artifact)
    with pytest.raises(FrozenInstanceError):
        artifact.password = "replacement"


@pytest.mark.parametrize("source", [None, 123, True, bytearray(b"pdf"), object()])
def test_bad_source_types_are_caller_errors(source):
    with pytest.raises(TypeError):
        NativePdfLayoutAnalyzer().analyze(source=source)


@pytest.mark.parametrize("password", [False, 123, b"bytes-not-supported"])
def test_password_type_is_checked_before_opening_source(password):
    with pytest.raises(TypeError, match="password"):
        NativePdfLayoutAnalyzer().analyze(source=Artifact(path="missing.pdf", password=password))


def test_analyzer_has_no_separate_password_parameter():
    with pytest.raises(TypeError, match="password"):
        NativePdfLayoutAnalyzer().analyze(source=b"pdf", password="secret")


@pytest.mark.parametrize("box", [
    (-1, 0, 10, 10), (10, 0, 5, 10), (0, 10, 10, 10),
    (0, 0, float("inf"), 10), (0, float("nan"), 10, 10),
])
def test_bad_geometry_is_rejected(box):
    with pytest.raises(ValidationError):
        LayoutRegion(id="r1", surface_id="p1", kind="text", bbox=box)


def test_result_is_serializable_and_frozen():
    page = LayoutSurface(id_="p1", location=PdfPageLocation(page_number=1), width=100, height=200)
    region = LayoutRegion(id="r1", surface_id="p1", kind="text", bbox=(5, 10, 90, 30))
    result = LayoutAnalysisResult(outcome="success", surfaces=(page,), regions=(region,))
    assert LayoutAnalysisResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.regions[0].kind = "table"
    assert not hasattr(result, "artifact")


@pytest.mark.parametrize("case", ["missing_surface", "outside", "duplicate_region", "duplicate_surface"])
def test_unresolvable_maps_are_rejected(case):
    page = LayoutSurface(id_="p1", location=PdfPageLocation(page_number=1), width=100, height=200)
    region = LayoutRegion(
        id="r1", surface_id="missing" if case == "missing_surface" else "p1",
        kind="image", bbox=(0, 0, 110 if case == "outside" else 50, 50),
    )
    with pytest.raises(ValidationError):
        LayoutAnalysisResult(
            outcome="success", surfaces=(page, page) if case == "duplicate_surface" else (page,),
            regions=(region, region) if case == "duplicate_region" else (region,),
        )


def test_partial_and_failed_are_distinct_from_blank_success():
    page = LayoutSurface(id_="p1", location=PdfPageLocation(page_number=1), width=100, height=200)
    finding = LayoutFinding(code="layout.pdf.test_gap", message="Page regions unavailable.")
    assert LayoutAnalysisResult(outcome="success", surfaces=(page,)).regions == ()
    assert LayoutAnalysisResult(outcome="partial", surfaces=(page,), findings=(finding,)).surfaces
    with pytest.raises(ValidationError):
        LayoutAnalysisResult(outcome="partial", surfaces=(page,))
    with pytest.raises(ValidationError):
        LayoutAnalysisResult(outcome="failed", surfaces=(page,), findings=(finding,))
