import json

from pydantic import ValidationError
import pytest

from raggae.documents.validation.schemas.report import DocumentValidationReport
from raggae.documents.validation.schemas.result import CheckRun


def load_check(data, mode):
    if mode == "constructor":
        return CheckRun(**data)
    if mode == "python":
        return CheckRun.model_validate(data)
    return CheckRun.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("mode", ["constructor", "python", "json"])
@pytest.mark.parametrize("reason", ["Password missing", "", " "])
def test_executed_check_rejects_any_skip_reason(mode, reason):
    with pytest.raises(ValidationError, match="A check that ran cannot have a skip reason"):
        load_check({"check": "worker", "ran": True, "skipped_because": reason}, mode)


@pytest.mark.parametrize("mode", ["constructor", "python", "json"])
@pytest.mark.parametrize("ran,reason", [
    (True, None),
    (False, None),
    (False, ""),
    (False, "Password missing"),
])
def test_valid_audit_pairs_preserve_optional_skip_reason(mode, ran, reason):
    check = load_check({"check": "worker", "ran": ran, "skipped_because": reason}, mode)
    assert check.ran is ran
    assert check.skipped_because == reason
    assert CheckRun.model_validate_json(check.model_dump_json()) == check


def test_executed_check_can_omit_skip_reason():
    assert CheckRun(check="worker", ran=True).skipped_because is None


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize("reason", ["Password missing", "", " "])
def test_report_rejects_contradictory_nested_audit(mode, reason):
    data = {
        "decision": "accept",
        "checks": [{"check": "worker", "ran": True, "skipped_because": reason}],
    }
    with pytest.raises(ValidationError, match="A check that ran cannot have a skip reason") as error:
        if mode == "python":
            DocumentValidationReport.model_validate(data)
        else:
            DocumentValidationReport.model_validate_json(json.dumps(data))
    assert error.value.errors()[0]["loc"] == ("checks", 0)
