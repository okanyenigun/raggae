import pytest

from raggae.documents.validation import Decision, DecisionPolicy, Severity
from raggae.documents.validation.schemas.result import CheckOutcome, Finding


STAGES = ("filename", "identity", "content_type", "encryption", "resource", "active_content", "capability")


def finding(code):
    return Finding(code=code, message=f"Controlled {code}.")


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("severity", [Severity.INFO, Severity.WARNING, Severity.REJECT])
def test_effective_severity_and_collection_control_stopping(inspector_factory, missing_path, stage, stop, severity):
    policy = DecisionPolicy(severity_by_code={"test.issue": severity}, stop_on_first_rejection=stop)
    assembly = inspector_factory(policy=policy)
    worker = assembly.workers[stage]
    issue = finding("test.issue")
    worker.outcome = CheckOutcome(findings=(issue,), facts={**worker.outcome.facts, "trigger_fact": stage})
    report = assembly.inspector.validate(missing_path)
    expected_stages = STAGES[:STAGES.index(stage) + 1] if stop and severity is Severity.REJECT else STAGES
    assert [call[0] for call in assembly.calls] == [f"test_{name}" for name in expected_stages]
    assert report.checks_that_ran == tuple(f"test_{name}" for name in expected_stages)
    assert tuple(weighted.finding for weighted in report.findings) == (issue,)
    assert report.findings[0].severity is severity
    assert report.facts["trigger_fact"] == stage
    assert report.decision is {
        Severity.INFO: Decision.ACCEPT, Severity.WARNING: Decision.ACCEPT_WITH_WARNINGS,
        Severity.REJECT: Decision.REJECT,
    }[severity]


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("reject_index", range(3))
def test_rejecting_outcome_keeps_all_its_findings_facts_and_audit(
    inspector_factory, missing_path, stage, reject_index,
):
    issues = (finding("test.info"), finding("test.warn"), finding("test.reject"))
    ordered = list(issues[:-1])
    ordered.insert(reject_index, issues[-1])
    policy = DecisionPolicy(severity_by_code={
        "test.info": Severity.INFO, "test.warn": Severity.WARNING, "test.reject": Severity.REJECT,
    })
    assembly = inspector_factory(policy=policy)
    worker = assembly.workers[stage]
    worker.outcome = CheckOutcome(findings=tuple(ordered), facts={**worker.outcome.facts, "evidence": stage})
    report = assembly.inspector.validate(missing_path)
    assert tuple(weighted.finding for weighted in report.findings) == tuple(ordered)
    assert tuple(weighted.severity for weighted in report.findings) == tuple(policy.severity_of(issue) for issue in ordered)
    assert report.facts["evidence"] == stage
    assert worker.name in report.checks_that_ran
    assert report.decision is Decision.REJECT
    assert len(assembly.calls) == STAGES.index(stage) + 1


@pytest.mark.parametrize("codes,expected", [
    (("test.recoverable",), Decision.NEEDS_PASSWORD),
    (("test.warn", "test.recoverable"), Decision.NEEDS_PASSWORD),
    (("test.recoverable", "test.recoverable"), Decision.NEEDS_PASSWORD),
    (("test.recoverable", "test.reject"), Decision.REJECT),
    (("test.reject", "test.recoverable"), Decision.REJECT),
    (("test.info", "test.warn"), Decision.ACCEPT_WITH_WARNINGS),
])
def test_collection_decision_combinations_preserve_all_findings(inspector_factory, missing_path, codes, expected):
    policy = DecisionPolicy(
        severity_by_code={"test.recoverable": Severity.REJECT, "test.reject": Severity.REJECT,
                          "test.warn": Severity.WARNING, "test.info": Severity.INFO},
        recoverable_codes=frozenset({"test.recoverable"}), stop_on_first_rejection=False,
    )
    assembly = inspector_factory(policy=policy)
    for stage, code in zip(STAGES, codes):
        worker = assembly.workers[stage]
        worker.outcome = CheckOutcome(findings=(finding(code),), facts=worker.outcome.facts)
    report = assembly.inspector.validate(missing_path)
    assert tuple(weighted.finding.code for weighted in report.findings) == codes
    assert report.decision is expected
    assert len(assembly.calls) == 7


@pytest.mark.parametrize("fallback", [Severity.INFO, Severity.WARNING, Severity.REJECT])
def test_unknown_code_uses_policy_fallback_for_weight_and_stopping(inspector_factory, missing_path, fallback):
    policy = DecisionPolicy(severity_by_code={}, default_severity=fallback)
    assembly = inspector_factory(policy=policy)
    worker = assembly.workers["filename"]
    worker.outcome = CheckOutcome(findings=(finding("custom.unknown"),), facts=worker.outcome.facts)
    report = assembly.inspector.validate(missing_path)
    assert report.findings[0].severity is fallback
    assert len(assembly.calls) == (1 if fallback is Severity.REJECT else 7)
    assert report.decision is {
        Severity.INFO: Decision.ACCEPT, Severity.WARNING: Decision.ACCEPT_WITH_WARNINGS,
        Severity.REJECT: Decision.REJECT,
    }[fallback]
