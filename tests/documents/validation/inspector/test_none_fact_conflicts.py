import pytest

from raggae.documents.validation.schemas.result import CheckOutcome


@pytest.mark.parametrize("later_value", ["reported", 0, False], ids=["text", "zero", "false"])
def test_explicit_none_fact_is_not_silently_overwritten(inspector_factory, missing_path, later_value):
    assembly = inspector_factory()
    first = assembly.workers["filename"]
    second = assembly.workers["identity"]
    first.outcome = CheckOutcome(facts={**first.outcome.facts, "shared_fact": None})
    second.outcome = CheckOutcome(facts={**second.outcome.facts, "shared_fact": later_value})
    assert "shared_fact" in first.outcome.facts

    with pytest.raises(RuntimeError) as raised:
        assembly.inspector.validate(missing_path)
    assert "shared_fact" in str(raised.value)
    assert "None" in str(raised.value)
    assert repr(later_value) in str(raised.value)
    assert [call[0] for call in assembly.calls] == ["test_filename", "test_identity"]
