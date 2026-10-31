"""Configuration constraints and sharing, independently of document parsing."""

import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    DocxLayoutAnalyzer, DocxLayoutPolicy, GeometricReadingOrderResolver,
    HtmlLayoutAnalyzer, HtmlLayoutPolicy, NativePdfLayoutAnalyzer, PdfLayoutPolicy,
    PdfRepeatedMarginDetector, PptxLayoutAnalyzer, PptxLayoutPolicy, ReadingOrderPolicy,
    RegionRelationshipPolicy, RepeatedMarginPolicy, RuleBasedRegionRelationshipDetector,
    VisualLayoutAnalyzer, VisualLayoutPolicy, XlsxLayoutAnalyzer, XlsxLayoutPolicy,
)


CASES = [
    (NativePdfLayoutAnalyzer, PdfLayoutPolicy, dict(
        max_source_bytes=50 * 1024 * 1024, max_pages=100, max_objects_per_page=10_000,
    )),
    (DocxLayoutAnalyzer, DocxLayoutPolicy, dict(
        max_source_bytes=50 * 1024 * 1024, max_expanded_bytes=100 * 1024 * 1024,
        max_regions=10_000, max_archive_entries=2_000,
    )),
    (PptxLayoutAnalyzer, PptxLayoutPolicy, dict(
        max_source_bytes=50 * 1024 * 1024, max_expanded_bytes=100 * 1024 * 1024,
        max_slides=100, max_shapes_per_slide=2_000, max_archive_entries=2_000, max_group_depth=32,
    )),
    (XlsxLayoutAnalyzer, XlsxLayoutPolicy, dict(
        max_source_bytes=50 * 1024 * 1024, max_expanded_bytes=100 * 1024 * 1024,
        max_sheets=100, max_cells_per_sheet=100_000, max_regions_per_sheet=2_000,
        max_archive_entries=2_000,
    )),
    (HtmlLayoutAnalyzer, HtmlLayoutPolicy, dict(
        max_source_bytes=50 * 1024 * 1024, max_elements=50_000, max_depth=128, max_regions=10_000,
    )),
    (VisualLayoutAnalyzer, VisualLayoutPolicy, dict(
        dpi=144, max_source_bytes=50 * 1024 * 1024, max_pages=100,
        max_pixels_per_page=20_000_000, max_regions_per_page=2_000,
    )),
    (GeometricReadingOrderResolver, ReadingOrderPolicy, dict(
        minimum_gap_ratio=0.005, minimum_spanning_width_ratio=0.7, max_partition_depth=64,
    )),
    (PdfRepeatedMarginDetector, RepeatedMarginPolicy, dict(
        margin_ratio=0.12, position_tolerance=0.03, minimum_pages=2, minimum_page_fraction=0.6,
        max_source_bytes=50 * 1024 * 1024, max_pages=100, max_objects_per_page=10_000,
        max_margin_regions=2_000,
    )),
    (RuleBasedRegionRelationshipDetector, RegionRelationshipPolicy, dict(
        maximum_caption_gap_ratio=0.04, maximum_caption_height_ratio=0.08,
        minimum_horizontal_overlap=0.5, ambiguity_gap_ratio=0.01,
        max_regions_per_surface=2_000, max_comparisons=100_000, max_relationships=10_000,
    )),
]


class EmptyService:
    def detect(self, image):
        return ()


def create(worker, **kwargs):
    if worker is VisualLayoutAnalyzer:
        kwargs["service"] = EmptyService()
    return worker(**kwargs)


@pytest.mark.parametrize("worker,policy_type,expected", CASES, ids=[case[1].__name__ for case in CASES])
def test_defaults_and_round_trip(worker, policy_type, expected):
    policy = policy_type()
    assert policy.model_dump() == expected
    assert create(worker).policy == policy
    assert policy_type.model_validate_json(policy.model_dump_json()) == policy
    assert set(policy_type.model_fields) == set(expected)
    assert all(field.description for field in policy_type.model_fields.values())


@pytest.mark.parametrize("worker,policy_type,expected", CASES, ids=[case[1].__name__ for case in CASES])
def test_explicit_policies_are_reusable_and_frozen(worker, policy_type, expected):
    field = next(iter(expected))
    value = 0.01 if isinstance(expected[field], float) else 10
    policy = policy_type(**{field: value})
    first, second = create(worker, policy=policy), create(worker, policy=policy)
    assert first.policy is second.policy is policy
    with pytest.raises(ValidationError, match="frozen"):
        setattr(policy, field, value)
    assert getattr(policy_type(), field) == expected[field]
    with pytest.raises(AttributeError):
        first.policy = policy_type()


FIELDS = [(policy_type, name, default) for _, policy_type, defaults in CASES for name, default in defaults.items()]


@pytest.mark.parametrize("policy_type,field,default", FIELDS)
@pytest.mark.parametrize("invalid", [True, False, "1", None, 0, -1, float("nan"), float("inf")])
def test_fields_reject_invalid_types_nonpositive_or_nonfinite_values(policy_type, field, default, invalid):
    with pytest.raises(ValidationError) as caught:
        policy_type(**{field: invalid})
    assert caught.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("policy_type,field,default", [case for case in FIELDS if isinstance(case[2], int)])
def test_integer_limits_do_not_truncate_fractional_values(policy_type, field, default):
    with pytest.raises(ValidationError):
        policy_type(**{field: 1.5})


@pytest.mark.parametrize("worker,policy_type,expected", CASES, ids=[case[1].__name__ for case in CASES])
def test_unknown_fields_cannot_silently_leave_defaults_in_effect(worker, policy_type, expected):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        policy_type(misspelled_limit=1)


@pytest.mark.parametrize("policy_type,field,value,valid", [
    (ReadingOrderPolicy, "minimum_gap_ratio", 1, False),
    (ReadingOrderPolicy, "minimum_gap_ratio", 0.999, True),
    (ReadingOrderPolicy, "minimum_spanning_width_ratio", 1, True),
    (ReadingOrderPolicy, "minimum_spanning_width_ratio", 1.01, False),
    (RepeatedMarginPolicy, "margin_ratio", 0.5, True),
    (RepeatedMarginPolicy, "margin_ratio", 0.501, False),
    (RepeatedMarginPolicy, "minimum_pages", 1, False),
    (RepeatedMarginPolicy, "minimum_pages", 2, True),
    (RepeatedMarginPolicy, "minimum_page_fraction", 1, True),
    (RepeatedMarginPolicy, "position_tolerance", 1.01, False),
    (RegionRelationshipPolicy, "minimum_horizontal_overlap", 1, True),
    (RegionRelationshipPolicy, "maximum_caption_gap_ratio", 1.01, False),
])
def test_ratio_and_support_boundaries(policy_type, field, value, valid):
    if valid:
        assert getattr(policy_type(**{field: value}), field) == value
    else:
        with pytest.raises(ValidationError):
            policy_type(**{field: value})
