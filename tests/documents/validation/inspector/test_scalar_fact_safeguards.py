import pytest

from raggae.documents.validation import Decision
from raggae.documents.validation.schemas.result import CheckOutcome


STAGES = ("filename", "identity", "content_type", "encryption", "resource", "active_content", "capability")
PAIRS = [(True, 1), (1, True), (False, 0), (0, False), (True, 1.0),
         (1.0, True), (False, 0.0), (0.0, False), (1, 1.0), (1.0, 1)]


@pytest.mark.parametrize("first_index", range(6), ids=STAGES[:-1])
@pytest.mark.parametrize("before,after", PAIRS)
def test_type_conflict_is_detected_at_each_stage_transition(
    inspector_factory, missing_path, first_index, before, after,
):
    assembly = inspector_factory()
    snapshots = []
    for stage, value in zip(STAGES[first_index:first_index + 2], (before, after), strict=True):
        worker = assembly.workers[stage]
        facts = {**worker.outcome.facts, "shared_fact": value}
        worker.outcome = CheckOutcome(facts=facts)
        assert type(worker.outcome.facts["shared_fact"]) is type(value)
        snapshots.append((worker.outcome, dict(facts)))
    with pytest.raises(RuntimeError, match="shared_fact"):
        assembly.inspector.validate(missing_path)
    assert [call[0] for call in assembly.calls] == [f"test_{stage}" for stage in STAGES[:first_index + 2]]
    assert all(dict(outcome.facts) == facts for outcome, facts in snapshots)


@pytest.mark.parametrize("value", ["hello", "", 7, 1.5, True, False, None])
def test_first_seen_scalar_fact_retains_its_type(inspector_factory, missing_path, value):
    assembly = inspector_factory()
    worker = assembly.workers["identity"]
    worker.outcome = CheckOutcome(facts={**worker.outcome.facts, "new_fact": value})
    report = assembly.inspector.validate(missing_path)
    assert report.decision is Decision.ACCEPT
    assert report.facts["new_fact"] == value
    assert type(report.facts["new_fact"]) is type(value)
    assert len(assembly.calls) == 7
