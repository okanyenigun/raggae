"""Grouped report status, safe aggregation, immutability and map references."""

from itertools import product

import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    DocumentLayoutReport,
    LayoutAnalysisResult,
    LayoutFinding,
    ReadingOrderEdge,
    ReadingOrderResult,
    RegionRelationship,
    RegionRelationshipResult,
    RepeatedMarginGroup,
    RepeatedMarginResult,
)
from .test_coordinator import mapped


@pytest.mark.parametrize("discovery", ["success", "partial"])
@pytest.mark.parametrize(
    "statuses",
    list(product((None, "success", "partial", "failed", "not_applicable"), repeat=3)),
)
def test_overall_outcome_combinations_and_serialization(discovery, statuses):
    models = (ReadingOrderResult, RegionRelationshipResult, RepeatedMarginResult)
    gap = LayoutFinding(code="example.refinement", message="Example refinement gap.")
    values = [
        None
        if status is None
        else model(outcome=status, findings=() if status == "success" else (gap,))
        for model, status in zip(models, statuses)
    ]
    report = DocumentLayoutReport(
        layout=mapped(outcome=discovery),
        reading_order=values[0],
        relationships=values[1],
        repeated_margins=values[2],
    )
    expected = (
        "partial"
        if discovery == "partial"
        or any(status in ("partial", "failed") for status in statuses)
        else "success"
    )
    assert report.outcome == expected
    assert report.model_dump()["outcome"] == expected
    assert DocumentLayoutReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_discovery_keeps_its_outcome_and_has_no_refinements(outcome):
    layout = LayoutAnalysisResult(
        outcome=outcome,
        findings=(LayoutFinding(code="example.unusable", message="No map."),),
    )
    report = DocumentLayoutReport(layout=layout)
    assert report.outcome == outcome and report.findings == layout.findings
    with pytest.raises(ValidationError, match="usable"):
        DocumentLayoutReport(
            layout=layout, reading_order=ReadingOrderResult(outcome="success")
        )


def test_identical_inherited_findings_are_deduplicated_but_distinct_contexts_remain():
    original = mapped(outcome="partial")
    first = LayoutFinding(
        code="example.refinement", message="A region gap.", region_id="a"
    )
    second = LayoutFinding(
        code="example.refinement", message="A region gap.", region_id="b"
    )
    report = DocumentLayoutReport(
        layout=original,
        reading_order=ReadingOrderResult(
            outcome="partial", findings=original.findings + (first,)
        ),
        relationships=RegionRelationshipResult(
            outcome="partial", findings=original.findings + (second,)
        ),
        repeated_margins=RepeatedMarginResult(
            outcome="partial", findings=original.findings
        ),
    )
    assert report.findings == original.findings + (first, second)
    assert len(report.model_dump()["findings"]) == 3
    with pytest.raises(ValidationError):
        report.layout = mapped()
    with pytest.raises(ValidationError, match="frozen_instance"):
        report.outcome = "success"


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "reading_order",
            ReadingOrderResult(
                outcome="success",
                edges=(
                    ReadingOrderEdge(
                        before_region_id="a",
                        after_region_id="missing",
                        basis="vertical_gap",
                    ),
                ),
            ),
        ),
        (
            "reading_order",
            ReadingOrderResult(
                outcome="partial",
                unresolved_region_ids=("missing",),
                findings=(LayoutFinding(code="example.gap", message="Gap."),),
            ),
        ),
        (
            "relationships",
            RegionRelationshipResult(
                outcome="success",
                relationships=(
                    RegionRelationship(
                        kind="contains",
                        source_region_id="a",
                        target_region_id="missing",
                        basis="bbox_containment",
                    ),
                ),
            ),
        ),
        (
            "relationships",
            RegionRelationshipResult(
                outcome="partial",
                unresolved_region_ids=("missing",),
                findings=(LayoutFinding(code="example.gap", message="Gap."),),
            ),
        ),
        (
            "repeated_margins",
            RepeatedMarginResult(
                outcome="success",
                groups=(
                    RepeatedMarginGroup(
                        kind="header",
                        basis="repeated_text",
                        region_ids=("a", "missing"),
                    ),
                ),
            ),
        ),
    ],
)
def test_refinements_cannot_reference_a_different_map(field, value):
    with pytest.raises(ValidationError, match="absent"):
        DocumentLayoutReport(layout=mapped(), **{field: value})


def test_valid_region_links_are_preserved_without_rewriting_the_map():
    layout = mapped()
    order = ReadingOrderResult(
        outcome="success",
        edges=(
            ReadingOrderEdge(
                before_region_id="a", after_region_id="b", basis="vertical_gap"
            ),
        ),
    )
    relationship = RegionRelationshipResult(
        outcome="success",
        relationships=(
            RegionRelationship(
                kind="contains",
                source_region_id="a",
                target_region_id="b",
                basis="bbox_containment",
            ),
        ),
    )
    margins = RepeatedMarginResult(
        outcome="success",
        groups=(
            RepeatedMarginGroup(
                kind="header", basis="repeated_text", region_ids=("a", "b")
            ),
        ),
    )
    report = DocumentLayoutReport(
        layout=layout,
        reading_order=order,
        relationships=relationship,
        repeated_margins=margins,
    )
    assert report.layout is layout and report.reading_order is order
    assert report.relationships is relationship and report.repeated_margins is margins
