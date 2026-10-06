import json

from pydantic import ValidationError
import pytest

from raggae.documents.validation.schemas.result import (
    CheckOutcome,
    CheckRun,
    Finding,
    Severity,
    ValidationRun,
    WeightedFinding,
)


def test_severity_values():
    assert {item.value for item in Severity} == {"info", "warning", "reject"}


@pytest.mark.parametrize("field", ["code", "message"])
def test_finding_rejects_empty_required_strings(field):
    data = {"code": "test.code", "message": "Explanation."}
    data[field] = ""
    with pytest.raises(ValidationError) as error:
        Finding(**data)
    assert error.value.errors()[0]["loc"] == (field,)


def test_finding_string_representation():
    assert str(Finding(code="test.code", message="Explanation.")) == "test.code: Explanation."


@pytest.mark.parametrize("model,field", [(Finding, "detail"), (CheckOutcome, "facts")])
def test_scalar_mappings_preserve_types_and_round_trip(model, field, scalar_facts):
    data = {field: scalar_facts}
    if model is Finding:
        data.update(code="test.code", message="Explanation.")
    instance = model(**data)
    assert dict(getattr(instance, field)) == scalar_facts
    for key, value in scalar_facts.items():
        assert type(getattr(instance, field)[key]) is type(value)
    dumped = instance.model_dump()
    assert isinstance(dumped[field], dict)
    assert model.model_validate(dumped) == instance
    assert json.loads(instance.model_dump_json())[field] == scalar_facts
    assert model.model_validate_json(instance.model_dump_json()) == instance


@pytest.mark.parametrize("model,field", [(Finding, "detail"), (CheckOutcome, "facts")])
@pytest.mark.parametrize("invalid_value", [[1], {"nested": 1}, object()])
def test_nested_or_unsupported_mapping_values_are_rejected(model, field, invalid_value):
    data = {field: {"invalid": invalid_value}}
    if model is Finding:
        data.update(code="test.code", message="Explanation.")
    with pytest.raises(ValidationError):
        model(**data)


@pytest.mark.parametrize("model,field", [(Finding, "detail"), (CheckOutcome, "facts")])
def test_supplied_mappings_are_copied_and_read_only(model, field):
    original = {"count": 1}
    data = {field: original}
    if model is Finding:
        data.update(code="test.code", message="Explanation.")
    instance = model(**data)
    original["count"] = 2
    assert getattr(instance, field)["count"] == 1
    with pytest.raises(TypeError):
        getattr(instance, field)["count"] = 3


@pytest.mark.parametrize("model,field", [(Finding, "detail"), (CheckOutcome, "facts")])
def test_empty_default_mappings_are_read_only(model, field):
    """Omitting a mapping must not bypass the schema's read-only contract."""
    instance = model(code="test.code", message="Explanation.") if model is Finding else model()
    assert dict(getattr(instance, field)) == {}
    with pytest.raises(TypeError):
        getattr(instance, field)["unexpected"] = True


@pytest.mark.parametrize("instance,field,new_value", [
    (Finding(code="test.code", message="Explanation."), "message", "Changed."),
    (CheckOutcome(), "findings", ()),
    (CheckRun(check="worker", ran=True), "ran", False),
    (WeightedFinding(finding=Finding(code="test.code", message="Explanation."), severity="info"),
     "severity", Severity.REJECT),
])
def test_result_models_reject_attribute_reassignment(instance, field, new_value):
    with pytest.raises(ValidationError) as error:
        setattr(instance, field, new_value)
    assert error.value.errors()[0]["type"] == "frozen_instance"


def test_check_outcome_preserves_order_duplicates_and_lookup(finding_factory):
    first, second = finding_factory("first"), finding_factory("second")
    outcome = CheckOutcome(findings=(first, second, first))
    assert outcome.findings == (first, second, first)
    assert outcome.codes == ("first", "second", "first")
    assert outcome.found("first")
    assert outcome.found("second")
    assert not outcome.found("absent")
    assert CheckOutcome().codes == ()
    assert not CheckOutcome().found("first")


def test_weighted_finding_converts_valid_severity_and_round_trips(finding_factory):
    finding = finding_factory(count=2)
    weighted = WeightedFinding(finding=finding, severity="warning")
    assert weighted.finding is finding
    assert weighted.severity is Severity.WARNING
    assert WeightedFinding.model_validate_json(weighted.model_dump_json()) == weighted


def test_weighted_finding_rejects_unknown_severity(finding_factory):
    with pytest.raises(ValidationError):
        WeightedFinding(finding=finding_factory(), severity="fatal")


@pytest.mark.parametrize("ran,reason", [(True, None), (False, "no probe configured")])
def test_check_run_preserves_audit_fields_and_round_trips(ran, reason):
    run = CheckRun(check="example worker", ran=ran, skipped_because=reason)
    assert run.check == "example worker"
    assert run.ran is ran
    assert run.skipped_because == reason
    assert CheckRun.model_validate_json(run.model_dump_json()) == run


def test_validation_runs_start_empty_and_do_not_share_mutable_state(finding_factory):
    first, second = ValidationRun(), ValidationRun()
    assert first.findings == first.checks == []
    assert first.facts == {}
    assert first.stopped is False
    first.findings.append(WeightedFinding(finding=finding_factory(), severity="reject"))
    first.checks.append(CheckRun(check="worker", ran=True))
    first.facts["count"] = 1
    first.stopped = True
    assert second.findings == second.checks == []
    assert second.facts == {}
    assert second.stopped is False
