import pytest

from raggae.documents.validation.schemas.result import CheckOutcome


@pytest.mark.parametrize("before,after", [
    pytest.param(True, 1, id="true-to-int"),
    pytest.param(1, True, id="int-to-true"),
    pytest.param(False, 0, id="false-to-int"),
    pytest.param(0, False, id="int-to-false"),
    pytest.param(True, 1.0, id="true-to-float"),
    pytest.param(1.0, True, id="float-to-true"),
    pytest.param(False, 0.0, id="false-to-float"),
    pytest.param(0.0, False, id="float-to-false"),
    pytest.param(1, 1.0, id="int-to-float"),
    pytest.param(1.0, 1, id="float-to-int"),
])
def test_equal_python_values_with_different_fact_types_do_not_silently_overwrite(
    inspector_factory, missing_path, before, after,
):
    assembly = inspector_factory()
    for stage, value in (("filename", before), ("identity", after)):
        worker = assembly.workers[stage]
        worker.outcome = CheckOutcome(facts={**worker.outcome.facts, "shared_fact": value})
        assert type(worker.outcome.facts["shared_fact"]) is type(value)

    with pytest.raises(RuntimeError) as raised:
        assembly.inspector.validate(missing_path)
    assert "shared_fact" in str(raised.value)
    assert repr(before) in str(raised.value)
    assert repr(after) in str(raised.value)
    assert [call[0] for call in assembly.calls] == ["test_filename", "test_identity"]
