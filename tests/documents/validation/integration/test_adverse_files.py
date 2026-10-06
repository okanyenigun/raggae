import os

import pikepdf
import pytest

from raggae.documents.validation import (
    ActiveContentPolicy, ArchiveLimits, ArchiveResourceProbe, Decision, DocumentInspector,
    EncryptionPolicy, FileIdentityProbe, HtmlActiveContentProbe, IdentityPolicy,
    ImageLimits, ImageResourceProbe, PdfEncryptionProbe, PdfLimits, PdfResourceProbe,
    TextLimits, TextResourceProbe,
)


def codes(report):
    return tuple(weighted.finding.code for weighted in report.findings)


@pytest.mark.parametrize("password,accept,expected,code", [
    (None, True, Decision.NEEDS_PASSWORD, "encryption.password_required"),
    ("", True, Decision.NEEDS_PASSWORD, "encryption.password_incorrect"),
    ("wrong", True, Decision.NEEDS_PASSWORD, "encryption.password_incorrect"),
    ("reader-secret", True, Decision.ACCEPT, None),
    ("owner-secret", True, Decision.ACCEPT, None),
    ("reader-secret", False, Decision.REJECT, "encryption.password_protected"),
])
def test_locked_pdf_default_stopping_and_password_policy(
    integration_pdf, password, accept, expected, code,
):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    before = path.read_bytes()
    report = DocumentInspector(encryption_probe=PdfEncryptionProbe(
        EncryptionPolicy(accept_password_protected=accept),
    )).validate(path, password=password)
    assert report.decision is expected
    assert codes(report) == (() if code is None else (code,))
    assert report.facts["encrypted"] is True
    assert report.facts["password_required"] is True
    assert len(report.checks_that_ran) == (7 if expected is Decision.ACCEPT else 4)
    if expected is not Decision.ACCEPT:
        assert "page_count" not in report.facts
        assert "has_extractable_text" not in report.facts
    serialized = report.model_dump_json()
    assert "reader-secret" not in serialized and "owner-secret" not in serialized
    assert path.read_bytes() == before


def test_filename_warning_plus_locked_pdf_still_requests_password(integration_pdf):
    path = integration_pdf(encryption=pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6))
    report = DocumentInspector().validate(path, submitted_filename="../upload.pdf")
    assert report.decision is Decision.NEEDS_PASSWORD
    assert codes(report) == ("filename.path_components", "encryption.password_required")
    assert len(report.checks_that_ran) == 4


@pytest.mark.parametrize("extension,payload,expected_codes", [
    ("html", b'<html><script src="https://evil.example/tracker.js"></script></html>',
     ("active_content.script", "active_content.remote_reference")),
    ("csv", b'name,value\nAlice,"=1+1"\n', ("active_content.spreadsheet_formula",)),
])
def test_active_text_payloads_warn_without_execution(file_factory, extension, payload, expected_codes):
    path = file_factory(f"active.{extension}", payload)
    before = path.read_bytes()
    report = DocumentInspector().validate(path)
    assert report.decision is Decision.ACCEPT_WITH_WARNINGS
    assert codes(report) == expected_codes
    assert len(report.checks_that_ran) == 5
    assert path.read_bytes() == before


@pytest.mark.parametrize("format_name", ["docx", "xlsx", "pptx"])
def test_macro_markers_warn_without_loading_macro(integration_office, format_name):
    tree = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[format_name]
    path = integration_office(format_name, extra_parts={f"{tree}/vbaProject.bin": b"inert-test-marker"})
    before = path.read_bytes()
    report = DocumentInspector().validate(path)
    assert report.decision is Decision.ACCEPT_WITH_WARNINGS
    assert codes(report) == ("content_type.subtype_assumed", "active_content.macro")
    assert path.read_bytes() == before


def test_remote_template_reports_template_and_reference(integration_office):
    relationship = '<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="https://evil.example/template.dotm" TargetMode="External"/></Relationships>'
    path = integration_office(extra_parts={"word/_rels/settings.xml.rels": relationship})
    report = DocumentInspector().validate(path)
    assert report.decision is Decision.ACCEPT_WITH_WARNINGS
    assert codes(report) == (
        "content_type.subtype_assumed", "active_content.remote_template", "active_content.remote_reference",
    )


@pytest.mark.parametrize("allowed", [False, True])
def test_remote_reference_allowlist_removes_only_reference_finding(file_factory, allowed):
    path = file_factory("linked.html", b'<html><a href="https://cdn.example/file">Link</a></html>')
    active = HtmlActiveContentProbe(ActiveContentPolicy(allowed_reference_hosts=frozenset({"cdn.example"}) if allowed else frozenset()))
    report = DocumentInspector(active_content_probes=(active,)).validate(path)
    assert report.decision is Decision.ACCEPT
    assert codes(report) == (() if allowed else ("active_content.remote_reference",))


def test_pdf_mislabel_routes_by_bytes_with_warning(integration_pdf):
    path = integration_pdf()
    report = DocumentInspector().validate(path, submitted_filename="claim.txt")
    assert report.decision is Decision.ACCEPT_WITH_WARNINGS
    assert codes(report) == ("content_type.extension_mismatch",)
    assert report.facts["claimed_extension"] == "txt"
    assert report.facts["detected_format"] == "pdf"
    assert report.facts["has_extractable_text"] is True
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("signature,code", [(b"MZ-inert", "content_type.not_allowed"), (b"\x00\x01\x02", "content_type.undetermined")])
def test_unacceptable_bytes_stop_before_format_parsers(file_factory, monkeypatch, forbidden_operation, signature, code):
    path = file_factory("claim.pdf", signature)
    inspector = DocumentInspector()
    for worker in (inspector._encryption_probe, *inspector._resource_probes, *inspector._active_content_probes, inspector._capability_probes):
        monkeypatch.setattr(worker, "check", forbidden_operation)
    report = inspector.validate(path)
    assert report.decision is Decision.REJECT
    assert code in codes(report)
    assert len(report.checks_that_ran) == 3


def test_dangerous_filename_stops_before_identity_open(file_factory, monkeypatch, forbidden_operation):
    path = file_factory("safe.txt")
    inspector = DocumentInspector()
    monkeypatch.setattr(inspector._identity_probe, "check", forbidden_operation)
    report = inspector.validate(path, submitted_filename="payload.exe.pdf")
    assert report.decision is Decision.REJECT
    assert "filename.dangerous_extension" in codes(report)
    assert len(report.checks_that_ran) == 1


@pytest.mark.parametrize("kind,code", [
    ("missing", "identity.not_found"), ("directory", "identity.not_a_regular_file"),
    ("symlink", "identity.symlink"), ("fifo", "identity.not_a_regular_file"),
    ("oversized", "identity.too_large"),
])
def test_identity_rejection_blocks_content_detection(tmp_path, monkeypatch, forbidden_operation, kind, code):
    path = tmp_path / "document.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.txt"
        target.write_bytes(b"safe text")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(b"123456")
    inspector = DocumentInspector(identity_probe=FileIdentityProbe(IdentityPolicy(max_bytes=5)))
    monkeypatch.setattr(inspector._content_type_detector, "check", forbidden_operation)
    report = inspector.validate(path)
    assert report.decision is Decision.REJECT
    assert codes(report) == (code,)
    assert len(report.checks_that_ran) == 2


def test_pdf_page_limit_blocks_active_and_capability_checks(integration_pdf, monkeypatch, forbidden_operation):
    path = integration_pdf(pages=2)
    inspector = DocumentInspector(resource_probes=(PdfResourceProbe(PdfLimits(max_pages=1)),))
    monkeypatch.setattr(inspector._active_content_probes[0], "check", forbidden_operation)
    monkeypatch.setattr(inspector._capability_probes, "check", forbidden_operation)
    report = inspector.validate(path)
    assert report.decision is Decision.REJECT
    assert codes(report) == ("resource_limits.too_many_pages",)
    assert report.facts["page_count"] == 2
    assert len(report.checks_that_ran) == 5


def test_archive_budget_blocks_relationship_decompression(integration_office, monkeypatch, forbidden_operation):
    path = integration_office()
    inspector = DocumentInspector(resource_probes=(ArchiveResourceProbe(ArchiveLimits(max_uncompressed_bytes=1)),))
    for worker in inspector._active_content_probes:
        monkeypatch.setattr(worker, "check", forbidden_operation)
    report = inspector.validate(path)
    assert report.decision is Decision.REJECT
    assert codes(report) == ("content_type.subtype_assumed", "resource_limits.expands_too_large")
    assert len(report.checks_that_ran) == 4


def test_image_pixel_budget_rejects_tiny_real_image(integration_image):
    path = integration_image()
    report = DocumentInspector(resource_probes=(ImageResourceProbe(ImageLimits(max_pixels=1)),)).validate(path)
    assert report.decision is Decision.REJECT
    assert codes(report) == ("resource_limits.too_many_pixels",)
    assert report.facts["image_pixels"] == 6


def test_json_depth_budget_rejects_before_any_later_check(file_factory):
    path = file_factory("nested.json", b"[[[0]]]")
    report = DocumentInspector(resource_probes=(TextResourceProbe(TextLimits(max_nesting_depth=2)),)).validate(path)
    assert report.decision is Decision.REJECT
    assert codes(report) == ("resource_limits.nesting_too_deep",)
    assert len(report.checks_that_ran) == 4


def test_pdf_launch_action_rejects_before_capability(integration_pdf, monkeypatch, forbidden_operation):
    path = integration_pdf(launch=True)
    inspector = DocumentInspector()
    monkeypatch.setattr(inspector._capability_probes, "check", forbidden_operation)
    report = inspector.validate(path)
    assert report.decision is Decision.REJECT
    assert "active_content.launch_action" in codes(report)
    assert len(report.checks_that_ran) == 6
