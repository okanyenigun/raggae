"""Evidence-labelled containment/caption candidates and conservative ambiguity."""

from raggae.documents.layout_analysis import RegionRelationshipPolicy

from io import BytesIO
import subprocess
import sys

import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    DocxBodyLocation, DocxElementLocation, HtmlLayoutAnalyzer, LayoutAnalysisResult,
    LayoutFinding, LayoutRegion, LayoutSurface, PdfPageLocation, PptxShapeLocation,
    PptxSlideLocation, RegionRelationship, RegionRelationshipDetector, RegionRelationshipResult,
    RuleBasedRegionRelationshipDetector, XlsxDrawingLocation, XlsxRangeLocation, XlsxSheetLocation,
)


def region(identifier, kind, box, surface="page"):
    return LayoutRegion(id=identifier, surface_id=surface, kind=kind, bbox=box)


def page_map(*regions):
    return LayoutAnalysisResult(outcome="success", surfaces=(LayoutSurface(
        id_="page", location=PdfPageLocation(page_number=1), width=1000, height=1000,
    ),), regions=regions)


def links(result):
    return {(link.kind, link.source_region_id, link.target_region_id, link.basis) for link in result.relationships}


def test_public_protocol_result_serialization_and_unchanged_map():
    layout = page_map(region("image", "image", (100, 100, 400, 300)),
                      region("caption", "text", (120, 310, 380, 330)),
                      region("unrelated", "text", (600, 500, 950, 600)))
    original = layout.model_dump_json()
    detector = RuleBasedRegionRelationshipDetector()
    result = detector.detect(layout)
    assert result.outcome == "success"
    assert links(result) == {("caption_candidate", "caption", "image", "proximity")}
    assert not result.unresolved_region_ids
    assert RegionRelationshipResult.model_validate_json(result.model_dump_json()) == result
    assert layout.model_dump_json() == original
    assert isinstance(detector, RegionRelationshipDetector)
    assert detector.name == "layout_analysis_rule_based_region_relationships"


@pytest.mark.parametrize("target_kind", ["image", "table"])
@pytest.mark.parametrize("caption_kind", ["text", "caption"])
@pytest.mark.parametrize("position", ["above", "below"])
def test_caption_candidates_above_and_below_supported_targets(target_kind, caption_kind, position):
    box = (120, 70, 380, 90) if position == "above" else (120, 310, 380, 330)
    result = RuleBasedRegionRelationshipDetector().detect(page_map(
        region("target", target_kind, (100, 100, 400, 300)), region("label", caption_kind, box),
    ))
    assert links(result) == {("caption_candidate", "label", "target", "proximity")}


@pytest.mark.parametrize("box, kind", [
    ((120, 350, 380, 370), "text"),  # too far
    ((500, 310, 700, 330), "text"),  # no horizontal overlap
    ((120, 310, 380, 410), "text"),  # too tall for a caption candidate
    ((120, 310, 380, 330), "heading"),  # heading is not a candidate caption
    ((120, 200, 380, 220), "text"),  # inside target, not a separated caption
])
def test_ineligible_proximity_does_not_create_caption_links(box, kind):
    result = RuleBasedRegionRelationshipDetector().detect(page_map(
        region("target", "image", (100, 100, 400, 300)), region("label", kind, box),
    ))
    assert not any(link.kind == "caption_candidate" for link in result.relationships)


def test_touching_caption_and_exact_gap_boundary_are_eligible():
    for top in (300, 340):
        layout = page_map(region("target", "image", (100, 100, 400, 300)), region("label", "text", (120, top, 380, top + 20)))
        assert any(link.kind == "caption_candidate" for link in RuleBasedRegionRelationshipDetector().detect(layout).relationships)


def test_strict_containment_includes_transitive_links_but_not_identical_boxes():
    layout = page_map(region("outer", "image", (100, 100, 500, 500)),
                      region("middle", "unknown", (120, 120, 480, 480)),
                      region("inner", "unknown", (150, 150, 200, 200)))
    assert links(RuleBasedRegionRelationshipDetector().detect(layout)) == {
        ("contains", "outer", "middle", "bbox_containment"),
        ("contains", "outer", "inner", "bbox_containment"),
        ("contains", "middle", "inner", "bbox_containment"),
    }
    identical = page_map(region("a", "image", (100, 100, 200, 200)), region("b", "unknown", (100, 100, 200, 200)))
    assert not RuleBasedRegionRelationshipDetector().detect(identical).relationships


def test_overlap_without_containment_is_not_a_contains_link():
    layout = page_map(region("a", "image", (100, 100, 300, 300)), region("b", "unknown", (200, 200, 400, 400)))
    assert not RuleBasedRegionRelationshipDetector().detect(layout).relationships


def test_two_equal_targets_leave_caption_and_targets_unresolved():
    layout = page_map(region("left", "image", (100, 100, 300, 300)),
                      region("right", "image", (320, 100, 520, 300)),
                      region("caption", "text", (100, 310, 520, 330)))
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "partial" and not result.relationships
    assert set(result.unresolved_region_ids) == {"left", "right", "caption"}
    assert result.findings[0].code == "layout.relationship.ambiguous_caption"
    assert result == RuleBasedRegionRelationshipDetector().detect(layout.model_copy(update={"regions": tuple(reversed(layout.regions))}))


def test_two_similarly_close_captions_leave_target_unresolved():
    layout = page_map(region("image", "image", (100, 100, 400, 300)),
                      region("above", "text", (120, 65, 380, 85)),
                      region("below", "text", (120, 310, 380, 330)))
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "partial" and not result.relationships
    assert set(result.unresolved_region_ids) == {"above", "below", "image"}


def test_clear_nearest_mutual_choice_and_explicit_caption_priority():
    layout = page_map(region("image", "image", (100, 100, 400, 300)),
                      region("above", "text", (120, 45, 380, 65)),
                      region("below", "text", (120, 310, 380, 330)))
    assert links(RuleBasedRegionRelationshipDetector().detect(layout)) == {("caption_candidate", "below", "image", "proximity")}
    explicit = page_map(region("image", "image", (100, 100, 400, 300)),
                        region("above", "caption", (120, 50, 380, 70)),
                        region("below", "text", (120, 305, 380, 325)))
    assert links(RuleBasedRegionRelationshipDetector().detect(explicit)) == {("caption_candidate", "above", "image", "proximity")}


def test_relationships_never_cross_surfaces():
    first = LayoutSurface(id_="p1", location=PdfPageLocation(page_number=1), width=1000, height=1000)
    second = LayoutSurface(id_="p2", location=PdfPageLocation(page_number=2), width=1000, height=1000)
    layout = LayoutAnalysisResult(outcome="success", surfaces=(first, second), regions=(
        region("image", "image", (100, 100, 400, 300), "p1"), region("caption", "caption", (120, 310, 380, 330), "p2"),
    ))
    assert not RuleBasedRegionRelationshipDetector().detect(layout).relationships


def test_real_html_sibling_caption_and_ambiguous_figure():
    pytest.importorskip("bs4")
    layout = HtmlLayoutAnalyzer().analyze(source=b'<figure><img src="fixture"><figcaption>Figure label</figcaption></figure>')
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "success"
    image, caption = layout.regions
    assert links(result) == {("caption_candidate", caption.id, image.id, "shared_parent")}
    layout = HtmlLayoutAnalyzer().analyze(source=b'<figure><img src="a"><img src="b"><figcaption>Shared label</figcaption></figure>')
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "partial" and not result.relationships
    assert set(result.unresolved_region_ids) == {region.id for region in layout.regions}


def test_html_ancestry_and_upstream_findings_are_preserved():
    pytest.importorskip("bs4")
    layout = HtmlLayoutAnalyzer().analyze(source=b'<div>Loose text<p>Paragraph</p></div>')
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    parent, child = layout.regions
    assert result.outcome == "partial" and result.findings == layout.findings
    assert links(result) == {("contains", parent.id, child.id, "native_ancestry")}


def test_real_docx_table_and_embedded_image_ancestry():
    docx = pytest.importorskip("docx")
    from PIL import Image
    from raggae.documents.layout_analysis import DocxLayoutAnalyzer

    document = docx.Document()
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.text = "Table content"
    with BytesIO() as picture, BytesIO() as encoded:
        Image.new("RGB", (8, 8), "blue").save(picture, format="PNG")
        picture.seek(0)
        cell.add_paragraph().add_run().add_picture(picture)
        document.save(encoded)
        layout = DocxLayoutAnalyzer().analyze(source=encoded.getvalue())
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    table, image = layout.regions
    assert links(result) == {("contains", table.id, image.id, "native_ancestry")}
    assert result.outcome == "success"


def test_spreadsheet_ranges_have_cell_containment_not_point_geometry():
    surface = LayoutSurface(id_="sheet", kind="sheet", location=XlsxSheetLocation(sheet_number=1, sheet_name="Sheet"))
    layout = LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(
        LayoutRegion(id="outer", surface_id="sheet", kind="table", location=XlsxRangeLocation(sheet_name="Sheet", cell_range="A1:D10")),
        LayoutRegion(id="inner", surface_id="sheet", kind="unknown", location=XlsxRangeLocation(sheet_name="Sheet", cell_range="B2:C3")),
    ))
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "success"
    assert links(result) == {("contains", "outer", "inner", "cell_range_containment")}


def test_drawing_anchor_alone_does_not_establish_full_containment():
    surface = LayoutSurface(id_="sheet", kind="sheet", location=XlsxSheetLocation(sheet_number=1, sheet_name="Sheet"))
    layout = LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(
        LayoutRegion(id="outer", surface_id="sheet", kind="table", location=XlsxRangeLocation(sheet_name="Sheet", cell_range="A1:D10")),
        LayoutRegion(id="drawing", surface_id="sheet", kind="image", location=XlsxDrawingLocation(sheet_name="Sheet", drawing_kind="image", drawing_index=0, anchor_cell="B2")),
    ))
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "partial" and not result.relationships
    assert result.unresolved_region_ids == ("drawing",)


def test_rotated_slide_regions_keep_native_ancestry_but_spatial_gaps():
    surface = LayoutSurface(id_="slide", kind="slide", location=PptxSlideLocation(slide_number=1, part="ppt/slides/slide1.xml"), width=720, height=540)
    layout = LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(
        LayoutRegion(id="group", surface_id="slide", kind="unknown", location=PptxShapeLocation(part="ppt/slides/slide1.xml", shape_id=1, shape_path=(0,))),
        LayoutRegion(id="child", surface_id="slide", kind="text", location=PptxShapeLocation(part="ppt/slides/slide1.xml", shape_id=2, shape_path=(0, 0))),
    ))
    result = RuleBasedRegionRelationshipDetector().detect(layout)
    assert result.outcome == "partial" and set(result.unresolved_region_ids) == {"group", "child"}
    assert links(result) == {("contains", "group", "child", "native_ancestry")}


def test_native_paths_in_different_parts_do_not_create_ancestry():
    # Test the rule directly: a valid flow map normally already enforces one part.
    first = LayoutRegion(id="a", surface_id="body", kind="unknown", location=DocxElementLocation(part="word/document.xml", child_indexes=(0,)))
    second = LayoutRegion(id="b", surface_id="body", kind="text", location=DocxElementLocation(part="word/header1.xml", child_indexes=(0, 0)))
    assert RuleBasedRegionRelationshipDetector._containment(first, second) is None


@pytest.mark.parametrize("option", ["max_regions_per_surface", "max_comparisons"])
def test_partial_comparison_does_not_choose_caption_from_incomplete_competitors(option):
    layout = page_map(region("image", "image", (100, 100, 400, 300)),
                      region("caption", "text", (120, 310, 380, 330)),
                      region("unrelated", "unknown", (600, 600, 800, 800)))
    result = RuleBasedRegionRelationshipDetector(policy=RegionRelationshipPolicy(**{option: 1 if option == "max_comparisons" else 2})).detect(layout)
    assert result.outcome == "partial" and not result.relationships
    assert set(result.unresolved_region_ids) == {region.id for region in layout.regions}


def test_relationship_output_limit_preserves_found_containment():
    layout = page_map(region("a", "unknown", (10, 10, 500, 500)), region("b", "unknown", (20, 20, 400, 400)), region("c", "unknown", (30, 30, 300, 300)))
    result = RuleBasedRegionRelationshipDetector(policy=RegionRelationshipPolicy(max_relationships=1)).detect(layout)
    assert result.outcome == "partial" and len(result.relationships) == 1
    assert any(finding.code == "layout.relationship.relationship_limit" for finding in result.findings)


def test_empty_layout_and_failed_input_are_preserved():
    empty = page_map()
    assert RuleBasedRegionRelationshipDetector().detect(empty) == RegionRelationshipResult(outcome="success")
    finding = LayoutFinding(code="test.failed", message="No usable map.")
    for outcome in ("failed", "not_applicable"):
        layout = LayoutAnalysisResult(outcome=outcome, findings=(finding,))
        result = RuleBasedRegionRelationshipDetector().detect(layout)
        assert result.outcome == outcome and result.findings == (finding,)
    with pytest.raises(TypeError):
        RuleBasedRegionRelationshipDetector().detect("not a layout")


@pytest.mark.parametrize("name", ["maximum_caption_gap_ratio", "maximum_caption_height_ratio", "minimum_horizontal_overlap", "ambiguity_gap_ratio"])
@pytest.mark.parametrize("value", [True, 0, -0.1, float("nan"), float("inf"), 1.1])
def test_invalid_ratios(name, value):
    with pytest.raises((TypeError, ValueError)):
        RuleBasedRegionRelationshipDetector(policy=RegionRelationshipPolicy(**{name: value}))


@pytest.mark.parametrize("name", ["max_regions_per_surface", "max_comparisons", "max_relationships"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_invalid_limits(name, value):
    with pytest.raises((TypeError, ValueError)):
        RuleBasedRegionRelationshipDetector(policy=RegionRelationshipPolicy(**{name: value}))


def test_models_are_frozen_and_reject_self_links_wrong_basis_and_duplicates():
    link = RegionRelationship(kind="contains", source_region_id="a", target_region_id="b", basis="native_ancestry")
    with pytest.raises(ValidationError):
        link.kind = "caption_candidate"
    for kwargs in ({"target_region_id": "a"}, {"source_region_id": ""}, {"basis": "proximity"}):
        with pytest.raises(ValidationError):
            RegionRelationship(**(link.model_dump() | kwargs))
    with pytest.raises(ValidationError):
        RegionRelationshipResult(outcome="success", relationships=(link, link))
    with pytest.raises(ValidationError):
        RegionRelationshipResult(outcome="success", unresolved_region_ids=("a",))
    with pytest.raises(ValidationError):
        RegionRelationshipResult(outcome="failed", relationships=(link,), findings=(LayoutFinding(code="test", message="Failed."),))


def test_public_import_does_not_load_optional_file_backends():
    code = '''
import builtins
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in ('bs4', 'docx', 'openpyxl', 'pptx', 'pdfplumber', 'pdfminer'):
        raise ImportError('file backends unavailable')
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from raggae.documents.layout_analysis import RuleBasedRegionRelationshipDetector
assert RuleBasedRegionRelationshipDetector().name == 'layout_analysis_rule_based_region_relationships'
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
