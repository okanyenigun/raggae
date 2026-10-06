import pytest

from raggae.documents.validation import Decision, DecisionPolicy, Severity
from raggae.documents.validation.schemas.result import CheckOutcome, Finding


STAGES = ("filename", "identity", "content_type", "encryption", "resource", "active_content", "capability")


def test_all_stages_contribute_disjoint_facts_without_mutating_outcomes(inspector_factory, missing_path):
    assembly = inspector_factory()
    originals = []
    expected = {"claimed_extension": "pdf", "size_bytes": 7, "detected_format": "pdf"}
    for index, (stage, worker) in enumerate(assembly.workers.items()):
        facts = {**worker.outcome.facts, f"fact_{stage}": index}
        worker.outcome = CheckOutcome(facts=facts)
        originals.append((worker.outcome, dict(facts)))
        expected.update(facts)
    report = assembly.inspector.validate(missing_path)
    assert dict(report.facts) == expected
    assert report.decision is Decision.ACCEPT
    assert len(report.checks_that_ran) == 7
    assert all(dict(outcome.facts) == facts for outcome, facts in originals)


@pytest.mark.parametrize("value", ["hello", "", 7, 1.5, True, None])
def test_identical_fact_from_multiple_workers_is_accepted(inspector_factory, missing_path, value):
    assembly = inspector_factory()
    for worker in assembly.workers.values():
        worker.outcome = CheckOutcome(facts={**worker.outcome.facts, "shared_fact": value})
    report = assembly.inspector.validate(missing_path)
    assert report.facts["shared_fact"] == value
    assert type(report.facts["shared_fact"]) is type(value)
    assert len(assembly.calls) == 7


@pytest.mark.parametrize("before,after", [("old", "new"), (7, 8), (1.5, 2.5), (True, False), ("", "nonempty")])
def test_conflicting_established_fact_raises_before_later_workers(inspector_factory, missing_path, before, after):
    assembly = inspector_factory()
    for stage, value in (("filename", before), ("identity", after)):
        worker = assembly.workers[stage]
        worker.outcome = CheckOutcome(facts={**worker.outcome.facts, "shared_fact": value})
    with pytest.raises(RuntimeError) as raised:
        assembly.inspector.validate(missing_path)
    assert "shared_fact" in str(raised.value)
    assert repr(before) in str(raised.value)
    assert repr(after) in str(raised.value)
    assert [call[0] for call in assembly.calls] == ["test_filename", "test_identity"]


@pytest.mark.parametrize("severity,expected_decision", [
    (Severity.INFO, Decision.ACCEPT),
    (Severity.WARNING, Decision.ACCEPT_WITH_WARNINGS),
    (Severity.REJECT, Decision.REJECT),
])
def test_findings_keep_stage_order_and_duplicates_using_inspector_severity(
    inspector_factory, missing_path, severity, expected_decision,
):
    policy = DecisionPolicy(severity_by_code={"test.shared": severity}, stop_on_first_rejection=False)
    assembly = inspector_factory(policy=policy)
    expected = []
    for stage, worker in assembly.workers.items():
        finding = Finding(code="test.shared", message=f"From {stage}.", detail={"stage": stage})
        worker.outcome = CheckOutcome(findings=(finding, finding), facts=worker.outcome.facts)
        expected.extend([finding, finding])
    report = assembly.inspector.validate(missing_path)
    assert tuple(weighted.finding for weighted in report.findings) == tuple(expected)
    assert tuple(weighted.severity for weighted in report.findings) == (severity,) * 14
    assert report.decision is expected_decision
    assert len(assembly.calls) == 7
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("error_factory", [RuntimeError, TypeError, OSError])
def test_worker_exception_propagates_and_blocks_later_workers(
    inspector_factory, missing_path, monkeypatch, stage, error_factory,
):
    assembly = inspector_factory()
    error = error_factory("controlled worker failure")
    worker = assembly.workers[stage]
    original_check = worker.check

    def fail(*args, **kwargs):
        original_check(*args, **kwargs)
        raise error

    monkeypatch.setattr(worker, "check", fail)
    with pytest.raises(error_factory) as raised:
        assembly.inspector.validate(missing_path)
    assert raised.value is error
    assert [call[0] for call in assembly.calls] == [f"test_{name}" for name in STAGES[:STAGES.index(stage) + 1]]
