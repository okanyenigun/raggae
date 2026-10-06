import pytest

from raggae.documents.validation.schemas.decision import Decision, DecisionPolicy
from raggae.documents.validation.schemas.result import CheckOutcome, Finding, Severity


STAGES = (
    "filename", "identity", "content_type", "encryption", "resource",
    "active_content", "capability",
)


@pytest.mark.parametrize("rejecting_stage", STAGES)
def test_early_rejection_leaves_later_checks_absent(inspector_factory, missing_path, rejecting_stage):
    setup = inspector_factory(
        policy=DecisionPolicy(severity_by_code={"test.reject": Severity.REJECT}),
    )
    worker = setup.workers[rejecting_stage]
    worker.outcome = CheckOutcome(
        findings=(Finding(code="test.reject", message="Reject this document."),),
        facts=worker.outcome.facts,
    )

    report = setup.inspector.validate(missing_path)

    executed = STAGES[:STAGES.index(rejecting_stage) + 1]
    expected_names = tuple(setup.workers[stage].name for stage in executed)
    assert report.decision is Decision.REJECT
    assert tuple(call[0] for call in setup.calls) == expected_names
    assert tuple(check.check for check in report.checks) == expected_names
    assert all(check.ran and check.skipped_because is None for check in report.checks)
    assert report.checks_that_did_not == ()
