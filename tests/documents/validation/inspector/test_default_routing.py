import pytest

from raggae.documents.validation import (
    ArchiveResourceProbe, CsvActiveContentProbe, Decision, DocumentInspector,
    HtmlActiveContentProbe, ImageResourceProbe, OoxmlActiveContentProbe,
    PdfActiveContentProbe, PdfCapabilityProbe, PdfEncryptionProbe, PdfResourceProbe,
    TextResourceProbe,
)
from raggae.documents.validation.schemas.result import CheckOutcome


PDF = (PdfEncryptionProbe, PdfResourceProbe, PdfActiveContentProbe, PdfCapabilityProbe)
OFFICE = (ArchiveResourceProbe, OoxmlActiveContentProbe)


@pytest.mark.parametrize("detected,canonical,expected", [
    ("pdf", "pdf", PDF), ("docx", "docx", OFFICE),
    ("xlsx", "xlsx", OFFICE), ("pptx", "pptx", OFFICE),
    ("png", "png", (ImageResourceProbe,)), ("jpeg", "jpeg", (ImageResourceProbe,)),
    ("tiff", "tiff", (ImageResourceProbe,)),
    ("csv", "csv", (TextResourceProbe, CsvActiveContentProbe)),
    ("html", "html", (TextResourceProbe, HtmlActiveContentProbe)),
    ("txt", "txt", (TextResourceProbe,)), ("md", "md", (TextResourceProbe,)),
    ("json", "json", (TextResourceProbe,)), ("jsonl", "jsonl", (TextResourceProbe,)),
    ("jpg", "jpeg", (ImageResourceProbe,)), ("tif", "tiff", (ImageResourceProbe,)),
    (" .PDF ", "pdf", PDF), (".DOCX", "docx", OFFICE),
    ("htm", "htm", ()), ("exe", "exe", ()), (None, None, ()),
])
def test_default_format_worker_selection_without_parsing(
    inspector_factory, missing_path, monkeypatch, detected, canonical, expected,
):
    # This isolates routing metadata, not real-file acceptance/content detection.
    assembly = inspector_factory()
    assembly.workers["content_type"].outcome = CheckOutcome(facts={"detected_format": detected})
    inspector = DocumentInspector(
        filename_validator=assembly.workers["filename"],
        identity_probe=assembly.workers["identity"],
        content_type_detector=assembly.workers["content_type"],
    )
    selected = []
    format_workers = (
        inspector._encryption_probe, *inspector._resource_probes,
        *inspector._active_content_probes, inspector._capability_probes,
    )
    for worker in format_workers:
        def check(*args, _worker=worker, **kwargs):
            selected.append((type(_worker), args, kwargs))
            return CheckOutcome()
        monkeypatch.setattr(worker, "check", check)

    report = inspector.validate(missing_path, password="secret")
    assert tuple(call[0] for call in selected) == expected
    assert all(call[1:] == ((missing_path, canonical, "secret"), {}) for call in selected)
    assert report.decision is Decision.ACCEPT
    assert report.findings == ()
    assert len(report.checks) == 7
    assert len(report.checks_that_ran) == 3 + len(expected)
    assert len(report.checks_that_did_not) == 4 - len(expected)
    for audit in report.checks:
        if not audit.ran:
            assert audit.skipped_because == (
                "format not determined" if canonical is None else f"no probe covers {canonical!r}"
            )
