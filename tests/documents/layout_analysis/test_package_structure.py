"""Category paths and the existing public imports resolve to the same workers."""

from importlib import import_module

import pytest

from raggae.documents import layout_analysis


@pytest.mark.parametrize(
    ("category", "folder", "worker_name", "policy_name"),
    [
        ("region_discovery", "pdf", "NativePdfLayoutAnalyzer", "PdfLayoutPolicy"),
        ("region_discovery", "docx", "DocxLayoutAnalyzer", "DocxLayoutPolicy"),
        ("region_discovery", "pptx", "PptxLayoutAnalyzer", "PptxLayoutPolicy"),
        ("region_discovery", "xlsx", "XlsxLayoutAnalyzer", "XlsxLayoutPolicy"),
        ("region_discovery", "html", "HtmlLayoutAnalyzer", "HtmlLayoutPolicy"),
        ("region_discovery", "visual", "VisualLayoutAnalyzer", "VisualLayoutPolicy"),
        (
            "map_analysis",
            "reading_order",
            "GeometricReadingOrderResolver",
            "ReadingOrderPolicy",
        ),
        (
            "map_analysis",
            "relationships",
            "RuleBasedRegionRelationshipDetector",
            "RegionRelationshipPolicy",
        ),
        (
            "map_analysis",
            "repeated_margin",
            "PdfRepeatedMarginDetector",
            "RepeatedMarginPolicy",
        ),
    ],
)
def test_categorized_workers_preserve_public_imports(
    category, folder, worker_name, policy_name
):
    prefix = f"raggae.documents.layout_analysis.{category}.{folder}"
    worker = getattr(import_module(prefix), worker_name)
    policy = getattr(import_module(f"{prefix}.policy"), policy_name)

    assert getattr(layout_analysis, worker_name) is worker
    assert getattr(layout_analysis, policy_name) is policy
    assert worker.__module__.startswith(prefix + ".")
