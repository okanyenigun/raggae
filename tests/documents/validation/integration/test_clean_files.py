import hashlib

import pytest

from raggae.documents.validation import (
    ArchiveResourceProbe, CsvActiveContentProbe, Decision, DocumentInspector,
    FileIdentityProbe, HtmlActiveContentProbe, ImageResourceProbe, OoxmlActiveContentProbe,
    PdfActiveContentProbe, PdfCapabilityProbe, PdfEncryptionProbe, PdfResourceProbe,
    SignatureContentTypeDetector, StrictFilenameValidator, TextResourceProbe,
)


BASE = (StrictFilenameValidator, FileIdentityProbe, SignatureContentTypeDetector)


def assert_unchanged_and_hashed(report, path, before):
    assert path.read_bytes() == before
    assert report.facts["digest"] == hashlib.sha256(before).hexdigest()
    assert report.facts["digest_algorithm"] == "sha256"
    assert report.facts["size_bytes"] == len(before)


@pytest.mark.parametrize("extension,content,active", [
    ("txt", b"Ordinary document text.", None),
    ("md", b"# Heading\nOrdinary document text.", None),
    ("json", b'{"key": "value"}', None),
    ("jsonl", b'{"key": 1}\n{"key": 2}\n', None),
    ("csv", b"name,amount\nAlice,42\n", CsvActiveContentProbe),
    ("html", b"<html><body>Ordinary document text.</body></html>", HtmlActiveContentProbe),
])
@pytest.mark.parametrize("string_path", [False, True])
def test_clean_text_formats(file_factory, extension, content, active, string_path):
    path = file_factory(f"document.{extension}", content)
    before = path.read_bytes()
    report = DocumentInspector().validate(str(path) if string_path else path)
    assert report.decision is Decision.ACCEPT
    assert report.findings == ()
    assert report.facts["detected_format"] == extension
    expected = (*BASE, TextResourceProbe, *((active,) if active else ()))
    assert report.checks_that_ran == tuple(factory().name for factory in expected)
    assert len(report.checks) == 7
    assert_unchanged_and_hashed(report, path, before)


@pytest.mark.parametrize("extension,format_name,canonical", [
    ("png", "PNG", "png"), ("jpeg", "JPEG", "jpeg"), ("jpg", "JPEG", "jpeg"),
    ("tiff", "TIFF", "tiff"), ("tif", "TIFF", "tiff"),
])
@pytest.mark.parametrize("string_path", [False, True])
def test_clean_images_and_aliases(integration_image, extension, format_name, canonical, string_path):
    path = integration_image(extension, format_name)
    before = path.read_bytes()
    report = DocumentInspector().validate(str(path) if string_path else path)
    assert report.decision is Decision.ACCEPT
    assert report.findings == ()
    assert report.facts["detected_format"] == canonical
    assert (report.facts["image_width"], report.facts["image_height"], report.facts["image_pixels"]) == (3, 2, 6)
    assert report.checks_that_ran == tuple(factory().name for factory in (*BASE, ImageResourceProbe))
    assert len(report.checks) == 7
    assert_unchanged_and_hashed(report, path, before)


@pytest.mark.parametrize("format_name", ["docx", "xlsx", "pptx"])
def test_clean_office_metadata_containers(integration_office, format_name):
    path = integration_office(format_name)
    before = path.read_bytes()
    report = DocumentInspector().validate(path)
    assert report.decision is Decision.ACCEPT
    assert tuple(weighted.finding.code for weighted in report.findings) == ("content_type.subtype_assumed",)
    assert report.facts["detected_format"] == format_name
    assert report.facts["archive_entries"] == 3
    assert report.checks_that_ran == tuple(factory().name for factory in (*BASE, ArchiveResourceProbe, OoxmlActiveContentProbe))
    assert_unchanged_and_hashed(report, path, before)


@pytest.mark.parametrize("kind,expected,has_text", [
    ("text", Decision.ACCEPT, True), ("scan", Decision.ACCEPT, False),
    ("blank", Decision.ACCEPT_WITH_WARNINGS, False),
])
def test_real_pdf_capabilities_and_repeatability(integration_pdf, kind, expected, has_text):
    path = integration_pdf(kind=kind)
    before = path.read_bytes()
    inspector = DocumentInspector()
    report = inspector.validate(path)
    assert report.decision is expected
    assert report.facts["has_extractable_text"] is has_text
    assert report.facts["page_count"] == report.facts["pages_sampled"] == 1
    assert tuple(weighted.finding.code for weighted in report.findings) == (("capability.empty_document",) if kind == "blank" else ())
    assert report.checks_that_ran == tuple(factory().name for factory in (
        *BASE, PdfEncryptionProbe, PdfResourceProbe, PdfActiveContentProbe, PdfCapabilityProbe,
    ))
    assert_unchanged_and_hashed(report, path, before)
    repeated = inspector.validate(path)
    assert repeated.model_dump() == report.model_dump()
    assert repeated is not report
