from raggae.documents.validation import FilenamePolicy
from raggae.documents.validation.shared import (
    DANGEROUS_EXTENSIONS,
    EXTENSION_ALIASES,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMATS,
    TEXT_FORMATS,
)


def test_canonical_supported_formats():
    assert SUPPORTED_FORMATS == frozenset({
        "txt", "md", "csv", "html", "json", "jsonl", "pdf",
        "docx", "xlsx", "pptx", "png", "jpeg", "tiff",
    })


def test_aliases_and_supported_extensions():
    assert EXTENSION_ALIASES == {"jpg": "jpeg", "tif": "tiff"}
    assert SUPPORTED_EXTENSIONS == SUPPORTED_FORMATS | {"jpg", "tif"}
    assert set(EXTENSION_ALIASES.values()) <= SUPPORTED_FORMATS


def test_text_family_is_supported():
    assert TEXT_FORMATS == {"txt", "md", "csv", "html", "json", "jsonl"}
    assert TEXT_FORMATS <= SUPPORTED_FORMATS


def test_dangerous_extensions_do_not_overlap_default_allowlist():
    assert DANGEROUS_EXTENSIONS == frozenset({
        "exe", "dll", "com", "bat", "cmd", "msi", "scr", "ps1", "vbs",
        "vbe", "js", "jse", "jar", "sh", "bash", "php", "phtml", "py",
        "rb", "pl", "cgi", "app", "dmg", "iso",
    })
    assert FilenamePolicy().allowed_extensions == SUPPORTED_EXTENSIONS
    assert not FilenamePolicy().allowed_extensions & DANGEROUS_EXTENSIONS
