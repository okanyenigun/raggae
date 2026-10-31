"""Document-level renderer export and backwards-compatible imports."""

import subprocess
import sys

import pytest


def test_region_renderer_is_exported_from_documents():
    from raggae import documents
    from raggae.documents import DocumentRegionRenderer
    from raggae.documents.utilities import DocumentRegionRenderer as utility_renderer

    assert DocumentRegionRenderer is utility_renderer
    assert "DocumentRegionRenderer" in documents.__all__


def test_reading_order_renderer_is_exported_from_documents():
    from raggae import documents
    from raggae.documents import DocumentReadingOrderRenderer
    from raggae.documents.utilities import (
        DocumentReadingOrderRenderer as utility_renderer,
    )

    assert DocumentReadingOrderRenderer is utility_renderer
    assert "DocumentReadingOrderRenderer" in documents.__all__


@pytest.mark.parametrize(
    "first_import",
    [
        "raggae.documents",
        "raggae.documents.utilities",
        "raggae.documents.layout_analysis",
        "raggae.documents.normalization",
        "raggae.documents.validation",
    ],
)
def test_document_export_supports_component_import_orders(first_import):
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import importlib
import sys

importlib.import_module({first_import!r})
from raggae.documents import DocumentRegionRenderer
from raggae.documents.utilities import DocumentRegionRenderer as utility_renderer

assert DocumentRegionRenderer is utility_renderer
assert not any(name in sys.modules for name in (
    'pypdfium2', 'IPython', 'paddleocr', 'paddle', 'torch', 'transformers',
))
""",
        ],
        check=True,
    )
