"""Dedicated worker packages preserve the shared public imports."""

from importlib import import_module
from pathlib import Path

import pytest

from raggae.documents import normalization
from raggae.documents.normalization import sanitization


@pytest.mark.parametrize("package_name,worker_name", [
    ("pdf_active_content", "PdfActiveContentSanitizer"),
    ("ooxml_active_content", "OoxmlActiveContentSanitizer"),
    ("html_active_content", "HtmlActiveContentSanitizer"),
    ("csv_formula", "CsvFormulaSanitizer"),
    ("pdf_metadata", "PdfMetadataSanitizer"),
    ("ooxml_metadata", "OoxmlMetadataSanitizer"),
    ("html_metadata", "HtmlMetadataSanitizer"),
    ("image_metadata", "ImageMetadataSanitizer"),
])
def test_worker_package_exports_resolve_to_the_same_public_classes(
    package_name, worker_name
):
    prefix = f"raggae.documents.normalization.sanitization.{package_name}"
    package = import_module(prefix)
    worker_module = import_module(f"{prefix}.worker")
    models_module = import_module(f"{prefix}.models")

    assert Path(package.__file__).name == "__init__.py"
    assert len(package.__all__) == len(set(package.__all__))
    assert worker_name in package.__all__
    for name in package.__all__:
        exported = getattr(package, name)
        origin = worker_module if name == worker_name else models_module
        assert exported is getattr(origin, name)
        assert exported is getattr(sanitization, name)
        assert exported is getattr(normalization, name)
