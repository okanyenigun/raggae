import pytest

from raggae.documents.validation import Decision, DecisionPolicy, Severity
from raggae.documents.validation.schemas.result import CheckOutcome, Finding, ValidationRun


REASON = "PDF access blocked: valid password required"
DEPENDENT = ("resource", "active_content", "capability")
PASSWORD_CODES = ("encryption.password_required", "encryption.password_incorrect")


def password_outcome(code):
    return CheckOutcome(findings=(Finding(code=code, message="Controlled password issue."),),
                        facts={"encrypted": True, "password_required": True})


@pytest.mark.parametrize("code", PASSWORD_CODES)
@pytest.mark.parametrize("severity", [Severity.INFO, Severity.WARNING, Severity.REJECT])
@pytest.mark.parametrize("stop", [False, True])
def test_pdf_access_prerequisite_blocks_calls_independently_of_decision_weight(
    inspector_factory, missing_path, monkeypatch, forbidden_operation, code, severity, stop,
):
    policy = DecisionPolicy(severity_by_code={code: severity}, stop_on_first_rejection=stop)
    assembly = inspector_factory(policy=policy)
    assembly.workers["encryption"].outcome = password_outcome(code)
    for stage in DEPENDENT:
        monkeypatch.setattr(assembly.workers[stage], "check", forbidden_operation)
    report = assembly.inspector.validate(missing_path)
    assert [call[0] for call in assembly.calls] == ["test_filename", "test_identity", "test_content_type", "test_encryption"]
    assert report.decision is {
        Severity.INFO: Decision.ACCEPT, Severity.WARNING: Decision.ACCEPT_WITH_WARNINGS,
        Severity.REJECT: Decision.NEEDS_PASSWORD,
    }[severity]
    assert tuple(weighted.finding.code for weighted in report.findings) == (code,)
    assert "pdf_access_blocked" not in report.facts
    if stop and severity is Severity.REJECT:
        assert len(report.checks) == 4  # Existing halted-stage audit contract.
    else:
        assert len(report.checks) == 7
        assert report.checks_that_did_not == tuple(f"test_{stage}" for stage in DEPENDENT)
        assert all(check.ran is False and check.skipped_because == REASON for check in report.checks[4:])


@pytest.mark.parametrize("code", [
    "encryption.password_protected", "encryption.weak_encryption",
    "encryption.extraction_not_permitted", "encryption.unreadable",
])
def test_other_encryption_findings_do_not_change_existing_collection_behavior(inspector_factory, missing_path, code):
    assembly = inspector_factory(policy=DecisionPolicy(stop_on_first_rejection=False))
    assembly.workers["encryption"].outcome = password_outcome(code)
    report = assembly.inspector.validate(missing_path)
    assert len(assembly.calls) == 7
    assert len(report.checks_that_ran) == 7
    assert tuple(weighted.finding.code for weighted in report.findings) == (code,)
    expected = Decision.ACCEPT_WITH_WARNINGS if code in {
        "encryption.weak_encryption", "encryption.extraction_not_permitted",
    } else Decision.REJECT
    assert report.decision is expected


@pytest.mark.parametrize("stage", ["filename", "identity", "resource"])
def test_only_encryption_outcome_establishes_pdf_access_block(inspector_factory, missing_path, stage):
    assembly = inspector_factory(policy=DecisionPolicy(stop_on_first_rejection=False))
    worker = assembly.workers[stage]
    worker.outcome = CheckOutcome(findings=password_outcome(PASSWORD_CODES[0]).findings, facts=worker.outcome.facts)
    report = assembly.inspector.validate(missing_path)
    assert len(assembly.calls) == 7
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("detected,canonical", [("html", "html"), (".DOCX", "docx")])
@pytest.mark.parametrize("code", PASSWORD_CODES)
def test_pdf_specific_prerequisite_does_not_block_other_format_routes(inspector_factory, missing_path, detected, canonical, code):
    assembly = inspector_factory(policy=DecisionPolicy(stop_on_first_rejection=False))
    assembly.workers["content_type"].outcome = CheckOutcome(facts={"detected_format": detected})
    assembly.workers["encryption"].outcome = password_outcome(code)
    for stage in ("encryption", *DEPENDENT):
        assembly.workers[stage].handles = frozenset({canonical})
    report = assembly.inspector.validate(missing_path)
    assert len(assembly.calls) == 7
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("dependency,label", [
    ("resource_probes", "resource limits"), ("active_content_probes", "active content"),
    ("capability_probes", "capability"),
])
@pytest.mark.parametrize("code", PASSWORD_CODES)
def test_config_and_coverage_skips_keep_priority_over_password_block(inspector_factory, missing_path, dependency, label, code):
    assembly = inspector_factory(**{
        "policy": DecisionPolicy(stop_on_first_rejection=False),
        **({dependency: ()} if dependency != "capability_probes" else {}),
    })
    if dependency == "capability_probes":
        assembly.workers["capability"].handles = frozenset({"html"})
    assembly.workers["encryption"].outcome = password_outcome(code)
    report = assembly.inspector.validate(missing_path)
    assert len(assembly.calls) == 4
    audit = next(check for check in report.checks if check.check == label)
    assert audit.ran is False
    assert audit.skipped_because == ("no probe covers 'pdf'" if dependency == "capability_probes" else "no probe configured")
    assert all(check.skipped_because == REASON for check in report.checks[4:] if check.check != label)


def test_pdf_access_flag_defaults_and_isolation():
    first, second = ValidationRun(), ValidationRun()
    assert first.pdf_access_blocked is second.pdf_access_blocked is False
    first.pdf_access_blocked = True
    assert second.pdf_access_blocked is False


@pytest.mark.parametrize("code", PASSWORD_CODES)
def test_custom_nonrecoverable_policy_does_not_remove_physical_access_guard(
    inspector_factory, missing_path, monkeypatch, forbidden_operation, code,
):
    policy = DecisionPolicy(stop_on_first_rejection=False, recoverable_codes=frozenset())
    assembly = inspector_factory(policy=policy)
    assembly.workers["encryption"].outcome = password_outcome(code)
    for stage in DEPENDENT:
        monkeypatch.setattr(assembly.workers[stage], "check", forbidden_operation)
    report = assembly.inspector.validate(missing_path)
    assert report.decision is Decision.REJECT
    assert len(assembly.calls) == 4
    assert all(check.skipped_because == REASON for check in report.checks[4:])
