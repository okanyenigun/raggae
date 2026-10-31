"""Verify geometric conventions and partial ordering, without semantic claims."""

from raggae.documents.layout_analysis import ReadingOrderPolicy

from itertools import permutations

import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    GeometricReadingOrderResolver, LayoutAnalysisResult, LayoutFinding, LayoutRegion,
    LayoutSurface, PdfPageLocation, NativePdfLayoutAnalyzer, ReadingOrderEdge,
    ReadingOrderResolver, ReadingOrderResult,
)


def layout(*boxes, findings=()):
    return LayoutAnalysisResult(
        outcome="partial" if findings else "success",
        surfaces=(LayoutSurface(id_="page-1", location=PdfPageLocation(page_number=1), width=600, height=800),),
        regions=tuple(LayoutRegion(id=name, surface_id="page-1", kind="text", bbox=box) for name, box in boxes),
        findings=findings,
    )


def pairs(result):
    return [(edge.before_region_id, edge.after_region_id) for edge in result.edges]


def test_partition_depth_policy_keeps_the_remaining_group_unresolved():
    document = layout(
        ("a", (10, 10, 590, 30)), ("b", (10, 100, 590, 120)), ("c", (10, 200, 590, 220)),
    )
    assert GeometricReadingOrderResolver().resolve_order(document).outcome == "success"
    limited = GeometricReadingOrderResolver(policy=ReadingOrderPolicy(max_partition_depth=1)).resolve_order(document)
    assert limited.outcome == "partial"
    assert set(limited.unresolved_region_ids) == {"b", "c"}


def test_single_column_uses_geometry_not_input_order():
    boxes = [("a", (40, 20, 550, 50)), ("b", (40, 120, 550, 180)), ("c", (40, 230, 550, 280))]
    resolver = GeometricReadingOrderResolver()
    for order in permutations(boxes):
        result = resolver.resolve_order(layout(*order))
        assert result.outcome == "success"
        assert pairs(result) == [("a", "b"), ("b", "c")]
        assert not result.unresolved_region_ids


@pytest.mark.parametrize("direction,expected", [
    ("ltr", [("a", "b"), ("b", "c"), ("c", "d")]),
    ("rtl", [("c", "d"), ("d", "a"), ("a", "b")]),
])
def test_columns_precede_rows_even_when_row_gap_is_wider(direction, expected):
    document = layout(
        ("d", (330, 250, 550, 290)), ("a", (40, 60, 260, 100)),
        ("c", (330, 60, 550, 100)), ("b", (40, 250, 260, 290)),
    )
    result = GeometricReadingOrderResolver().resolve_order(document, direction=direction)
    assert pairs(result) == expected
    assert result.outcome == "success"
    assert any(edge.basis == "vertical_gap" for edge in result.edges)


def test_full_width_title_and_footer_surround_columns():
    result = GeometricReadingOrderResolver().resolve_order(layout(
        ("title", (20, 10, 580, 30)), ("a", (40, 60, 260, 100)),
        ("b", (40, 250, 260, 290)), ("c", (330, 60, 550, 100)),
        ("d", (330, 250, 550, 290)), ("footer", (20, 730, 580, 750)),
    ))
    assert pairs(result) == [("title", "a"), ("a", "b"), ("b", "c"), ("c", "d"), ("d", "footer")]


def test_spanning_table_separates_two_column_sections():
    document = layout(
        ("left_top", (40, 40, 260, 100)), ("right_top", (330, 40, 550, 100)),
        ("table", (20, 200, 580, 350)),
        ("left_bottom", (40, 450, 260, 510)), ("right_bottom", (330, 450, 550, 510)),
    )
    # Kinds do not change the geometric decision; a table is one atomic area.
    regions = tuple(region.model_copy(update={"kind": "table"}) if region.id == "table" else region for region in document.regions)
    document = document.model_copy(update={"regions": regions})
    result = GeometricReadingOrderResolver().resolve_order(document)
    assert pairs(result) == [
        ("left_top", "right_top"), ("right_top", "table"),
        ("table", "left_bottom"), ("left_bottom", "right_bottom"),
    ]


def test_overlaps_keep_known_surrounding_relations_without_forcing_internal_order():
    document = layout(
        ("title", (20, 10, 580, 30)), ("image", (40, 100, 400, 300)),
        ("overlay", (60, 150, 280, 180)), ("footer", (20, 730, 580, 750)),
    )
    original = document.model_dump_json()
    result = GeometricReadingOrderResolver().resolve_order(document)
    assert result.outcome == "partial"
    assert set(result.unresolved_region_ids) == {"image", "overlay"}
    assert set(pairs(result)) == {("title", "image"), ("title", "overlay"), ("image", "footer"), ("overlay", "footer")}
    assert result.findings[0].code == "layout.order.unresolved"
    assert document.model_dump_json() == original


def test_touching_or_tiny_gaps_stay_unresolved():
    document = layout(("a", (40, 20, 500, 40)), ("b", (40, 40.1, 500, 60)))
    result = GeometricReadingOrderResolver().resolve_order(document)
    assert result.outcome == "partial" and result.edges == ()
    assert result.unresolved_region_ids == ("a", "b")


def test_page_order_skips_blank_surfaces_and_does_not_sort_region_ids():
    surfaces = tuple(LayoutSurface(id_=f"p{n}", location=PdfPageLocation(page_number=n), width=600, height=800) for n in (1, 2, 3))
    regions = (
        LayoutRegion(id="z", surface_id="p1", kind="text", bbox=(10, 10, 100, 30)),
        LayoutRegion(id="a", surface_id="p3", kind="text", bbox=(10, 10, 100, 30)),
    )
    result = GeometricReadingOrderResolver().resolve_order(LayoutAnalysisResult(outcome="success", surfaces=surfaces, regions=regions))
    assert pairs(result) == [("z", "a")]
    assert result.edges[0].basis == "page_order"
    assert GeometricReadingOrderResolver().resolve_order(LayoutAnalysisResult(outcome="success", surfaces=surfaces)).edges == ()


def test_analysis_coverage_gaps_propagate_without_becoming_ordering_success():
    finding = LayoutFinding(code="layout.pdf.image_content_unanalyzed", message="Pixels remain unanalyzed.")
    document = layout(("image", (20, 20, 580, 750)), findings=(finding,))
    result = GeometricReadingOrderResolver().resolve_order(document)
    assert result.outcome == "partial"
    assert result.findings == (finding,) and not result.unresolved_region_ids


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_analysis_stays_unusable(outcome):
    finding = LayoutFinding(code="layout.pdf.unavailable", message="No map available.")
    result = GeometricReadingOrderResolver().resolve_order(LayoutAnalysisResult(outcome=outcome, findings=(finding,)))
    assert result.outcome == outcome and result.edges == ()


def test_protocol_serialization_and_no_worker_metadata():
    resolver = GeometricReadingOrderResolver()
    assert isinstance(resolver, ReadingOrderResolver)
    assert resolver.name == "layout_analysis_geometric_reading_order"
    assert not hasattr(resolver, "implementation") and not hasattr(resolver, "version")
    result = resolver.resolve_order(layout(("a", (10, 10, 100, 30))))
    assert ReadingOrderResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("edges", [
    [("a", "b"), ("b", "a")], [("a", "b"), ("a", "b")],
])
def test_cycle_and_duplicate_edges_rejected(edges):
    with pytest.raises(ValidationError):
        ReadingOrderResult(outcome="success", edges=tuple(ReadingOrderEdge(before_region_id=a, after_region_id=b, basis="horizontal_gap") for a, b in edges))


@pytest.mark.parametrize("value", [0, 1, -0.1, float("nan"), float("inf"), True, "wide"])
def test_bad_gap_configuration_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        GeometricReadingOrderResolver(policy=ReadingOrderPolicy(minimum_gap_ratio=value))


def test_invalid_inputs_are_caller_errors():
    with pytest.raises(TypeError):
        GeometricReadingOrderResolver().resolve_order({})
    with pytest.raises(ValueError):
        GeometricReadingOrderResolver().resolve_order(layout(), direction="automatic")


def test_real_pdf_analyzer_feeds_geometric_ordering(invoice_pdf):
    pytest.importorskip("pdfplumber")
    document = NativePdfLayoutAnalyzer().analyze(source=invoice_pdf)
    result = GeometricReadingOrderResolver().resolve_order(document)
    assert result.outcome == "success"
    assert not result.unresolved_region_ids
    assert len(result.edges) == len(document.regions) - 1
    assert {identifier for pair in pairs(result) for identifier in pair} == {region.id for region in document.regions}
