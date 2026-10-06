"""The SDK entry point should expose the documented current names."""

from importlib import import_module
from pathlib import Path

import pytest

import raggae.documents.validation as validation


EXPORTS = {
    "StrictFilenameValidator": "checks.filename.strict",
    "FileIdentityProbe": "checks.identity.file",
    "SignatureContentTypeDetector": "checks.content_type.signature",
    "PdfEncryptionProbe": "checks.encryption.pdf",
    "ArchiveResourceProbe": "checks.resource_limits.archieve",
    "ImageResourceProbe": "checks.resource_limits.image",
    "PdfResourceProbe": "checks.resource_limits.pdf",
    "TextResourceProbe": "checks.resource_limits.text",
    "CsvActiveContentProbe": "checks.active_content.csv",
    "HtmlActiveContentProbe": "checks.active_content.html",
    "OoxmlActiveContentProbe": "checks.active_content.ooxml",
    "PdfActiveContentProbe": "checks.active_content.pdf",
    "PdfCapabilityProbe": "checks.capability.pdf",
    "DocumentInspector": "coordinator",
    "Decision": "schemas.decision",
    "DecisionPolicy": "schemas.decision",
    "Severity": "schemas.result",
    "DocumentValidationReport": "schemas.report",
    "FilenamePolicy": "checks.filename.policy",
    "IdentityPolicy": "checks.identity.policy",
    "ContentTypePolicy": "checks.content_type.policy",
    "EncryptionPolicy": "checks.encryption.policy",
    "PdfLimits": "checks.resource_limits.policy",
    "ArchiveLimits": "checks.resource_limits.policy",
    "ImageLimits": "checks.resource_limits.policy",
    "TextLimits": "checks.resource_limits.policy",
    "ActiveContentPolicy": "checks.active_content.policy",
    "CapabilityPolicy": "checks.capability.policy",
}


def test_all_lists_exactly_the_documented_exports_without_duplicates():
    assert set(validation.__all__) == set(EXPORTS)
    assert len(validation.__all__) == len(EXPORTS) == 28


@pytest.mark.parametrize("name,module_suffix", EXPORTS.items())
def test_public_export_is_the_defining_class(name, module_suffix):
    module = import_module(f"raggae.documents.validation.{module_suffix}")
    namespace = {}
    exec(f"from raggae.documents.validation import {name}", namespace)
    assert namespace[name] is getattr(module, name)
    assert getattr(validation, name).__name__ == name


PACKAGE_ROOT = Path(validation.__file__).parent
MODULE_NAMES = sorted(
    ".".join(
        ("raggae", "documents", "validation")
        + (path.relative_to(PACKAGE_ROOT).parent.parts
           if path.name == "__init__.py"
           else path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    )
    for path in PACKAGE_ROOT.rglob("*.py")
)


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_current_validation_module_imports(module_name):
    assert import_module(module_name).__name__ == module_name
