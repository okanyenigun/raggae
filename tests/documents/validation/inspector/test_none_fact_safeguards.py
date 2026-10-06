import pytest

from raggae.documents.validation import Decision
from raggae.documents.validation.schemas.result import CheckOutcome


STAGES = ("filename", "identity", "content_type", "encryption", "resource", "active_content", "capability")


@pytest.mark.parametrize("first_index", range(6), ids=STAGES[:-1])
@pytest.mark.parametrize("value", ["reported", 0, False], ids=["text", "zero", "false"])
@pytest.mark.parametrize("none_first", [True, False], ids=["none-first", "none-second"])
def test_none_disagreement_is_detected_in_both_directions_at_each_transition(
    inspector_factory, missing_path, first_index, value, none_first,
):
    assembly = inspector_factory()
    values = (None, value) if none_first else (value, None)
    snapshots = []
    for stage, fact in zip(STAGES[first_index:first_index + 2], values, strict=True):
        worker = assembly.workers[stage]
        facts = {**worker.outcome.facts, "shared_fact": fact}
        worker.outcome = CheckOutcome(facts=facts)
        snapshots.append((worker.outcome, dict(facts)))

    with pytest.raises(RuntimeError) as raised:
        assembly.inspector.validate(missing_path)
    assert "shared_fact" in str(raised.value)
    assert "None" in str(raised.value)
    assert repr(value) in str(raised.value)
    assert [call[0] for call in assembly.calls] == [f"test_{stage}" for stage in STAGES[:first_index + 2]]
    assert all(dict(outcome.facts) == facts for outcome, facts in snapshots)


@pytest.mark.parametrize("stage", STAGES)
def test_new_none_valued_fact_is_allowed_and_preserved(inspector_factory, missing_path, stage):
    assembly = inspector_factory()
    worker = assembly.workers[stage]
    worker.outcome = CheckOutcome(facts={**worker.outcome.facts, "new_fact": None})
    report = assembly.inspector.validate(missing_path)
    assert report.decision is Decision.ACCEPT
    assert "new_fact" in report.facts
    assert report.facts["new_fact"] is None
    assert len(assembly.calls) == 7
