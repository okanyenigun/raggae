import pikepdf
import pytest

from raggae.documents.validation import (
    ActiveContentPolicy, ArchiveLimits, ArchiveResourceProbe, ContentTypePolicy,
    Decision, DecisionPolicy, DocumentInspector, HtmlActiveContentProbe, Severity,
    SignatureContentTypeDetector,
)


def codes(report):
    return tuple(weighted.finding.code for weighted in report.findings)


@pytest.mark.parametrize("copy_defaults", [False, True])
@pytest.mark.parametrize("script_severity", [Severity.INFO, Severity.WARNING, Severity.REJECT])
def test_replacement_map_uses_fallback_while_copied_map_retains_other_defaults(
    file_factory, copy_defaults, script_severity,
):
    path = file_factory("linked.html", b'<html><script src="https://evil.example/code.js"></script></html>')
    severities = dict(DecisionPolicy().severity_by_code) if copy_defaults else {}
    severities["active_content.script"] = script_severity
    report = DocumentInspector(policy=DecisionPolicy(severity_by_code=severities)).validate(path)
    assert codes(report) == ("active_content.script", "active_content.remote_reference")
    expected_reference = Severity.INFO if copy_defaults else Severity.WARNING
    assert tuple(weighted.severity for weighted in report.findings) == (script_severity, expected_reference)
    expected = Decision.REJECT if script_severity is Severity.REJECT else (
        Decision.ACCEPT_WITH_WARNINGS if Severity.WARNING in (script_severity, expected_reference) else Decision.ACCEPT
    )
    assert report.decision is expected


@pytest.mark.parametrize("inject_worker_severities", [False, True])
def test_custom_worker_severities_need_explicit_inspector_map(file_factory, inject_worker_severities):
    path = file_factory("script.html", b"<html><script>inert()</script></html>")
    worker_policy = ActiveContentPolicy()
    worker_policy.severities["active_content.script"] = Severity.REJECT
    decision = None
    if inject_worker_severities:
        severities = dict(DecisionPolicy().severity_by_code)
        severities.update(worker_policy.severities)
        decision = DecisionPolicy(severity_by_code=severities)
    report = DocumentInspector(active_content_probes=(HtmlActiveContentProbe(worker_policy),), policy=decision).validate(path)
    assert codes(report) == ("active_content.script",)
    assert report.findings[0].severity is (Severity.REJECT if inject_worker_severities else Severity.WARNING)
    assert report.decision is (Decision.REJECT if inject_worker_severities else Decision.ACCEPT_WITH_WARNINGS)


@pytest.mark.parametrize("resource_disabled", [False, True])
@pytest.mark.parametrize("active_disabled", [False, True])
def test_explicitly_disabled_groups_are_audited_not_silently_enabled(
    file_factory, resource_disabled, active_disabled,
):
    path = file_factory("script.html", b"<html><script>inert()</script></html>")
    report = DocumentInspector(
        resource_probes=() if resource_disabled else None,
        active_content_probes=() if active_disabled else None,
    ).validate(path)
    assert report.decision is (Decision.ACCEPT if active_disabled else Decision.ACCEPT_WITH_WARNINGS)
    assert codes(report) == (() if active_disabled else ("active_content.script",))
    assert len(report.checks) == 7
    assert len(report.checks_that_ran) == 3 + int(not resource_disabled) + int(not active_disabled)
    for disabled, label in ((resource_disabled, "resource limits"), (active_disabled, "active content")):
        if disabled:
            audit = next(check for check in report.checks if check.check == label)
            assert audit.ran is False
            assert audit.skipped_because == "no probe configured"


@pytest.mark.parametrize("require_known", [False, True])
def test_explicit_unknown_format_policy_controls_unroutable_acceptance(file_factory, require_known):
    # Permissive acceptance is a deliberate caller choice, not full format validation.
    path = file_factory("unknown.txt", b"\x00\x01\x02")
    detector = SignatureContentTypeDetector(ContentTypePolicy(require_known_format=require_known))
    report = DocumentInspector(content_type_detector=detector).validate(path)
    assert report.decision is (Decision.REJECT if require_known else Decision.ACCEPT)
    assert codes(report) == (("content_type.undetermined",) if require_known else ())
    assert "detected_format" not in report.facts
    assert len(report.checks_that_ran) == 3
    if not require_known:
        assert len(report.checks_that_did_not) == 4
        assert all(check.skipped_because == "format not determined" for check in report.checks[3:])


@pytest.mark.parametrize("stop", [False, True])
def test_resource_rejection_then_macro_marker_respects_collection_choice(integration_office, stop):
    # This is a tiny fixture over a tiny custom limit, not a real expansion bomb.
    path = integration_office(extra_parts={"word/vbaProject.bin": b"inert-test-marker"})
    report = DocumentInspector(
        resource_probes=(ArchiveResourceProbe(ArchiveLimits(max_uncompressed_bytes=1)),),
        policy=DecisionPolicy(stop_on_first_rejection=stop),
    ).validate(path)
    assert report.decision is Decision.REJECT
    expected = ("content_type.subtype_assumed", "resource_limits.expands_too_large")
    assert codes(report) == (expected if stop else (*expected, "active_content.macro"))
    assert len(report.checks_that_ran) == (4 if stop else 5)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("advisory", ["weak", "extraction"])
def test_encryption_advisory_defaults_warn_and_explicit_policy_can_reject(integration_pdf, strict, advisory):
    if advisory == "weak":
        encryption = pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=2, aes=False, metadata=False)
        code = "encryption.weak_encryption"
    else:
        encryption = pikepdf.Encryption(user="reader-secret", owner="owner-secret", R=6,
                                       allow=pikepdf.Permissions(extract=False))
        code = "encryption.extraction_not_permitted"
    path = integration_pdf(encryption=encryption)
    before = path.read_bytes()
    policy = DecisionPolicy()
    if strict:
        policy.severity_by_code[code] = Severity.REJECT
    report = DocumentInspector(policy=policy).validate(path, password="reader-secret")
    assert codes(report) == (code,)
    assert report.findings[0].severity is (Severity.REJECT if strict else Severity.WARNING)
    assert report.decision is (Decision.REJECT if strict else Decision.ACCEPT_WITH_WARNINGS)
    assert len(report.checks_that_ran) == (4 if strict else 7)
    if advisory == "extraction":
        assert report.facts["extraction_allowed"] is False
    assert path.read_bytes() == before
