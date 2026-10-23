"""Result integrity, JSON serialization, and public imports."""

import json

from pydantic import ValidationError
import pytest

import raggae.documents.normalization as normalization
import raggae.documents.normalization.text_encoding as text_encoding
from raggae.documents.normalization import (
    TextEncodingFinding,
    TextEncodingIssue,
    TextEncodingOutcome,
    TextEncodingReport,
)


def _issue():
    return TextEncodingIssue(
        code=TextEncodingFinding.DECODE_FAILED,
        message="Source encoding could not be decoded.",
        detail={"encoding": "ascii"},
    )


def test_public_exports_match_the_component_exports():
    assert set(text_encoding.__all__).issubset(normalization.__all__)
    for name in text_encoding.__all__:
        assert getattr(normalization, name) is getattr(text_encoding, name)


def test_outcomes_and_finding_codes_are_stable():
    assert {item.value for item in TextEncodingOutcome} == {"no_change", "changed", "failed"}
    assert {item.value for item in TextEncodingFinding} == {
        "normalization.text.encoding_required", "normalization.text.decode_failed",
        "normalization.text.encode_failed",
    }


@pytest.mark.parametrize("result", [
    TextEncodingReport(outcome="no_change", source_encoding="utf-8"),
    TextEncodingReport(outcome="changed", source_encoding="cp1252"),
    TextEncodingReport(outcome="changed", source_encoding="utf-16-le", bom_removed=True),
    TextEncodingReport(outcome="failed", issues=(_issue(),)),
])
def test_results_round_trip_as_serializable_data_without_streams(result):
    assert TextEncodingReport.model_validate(result.model_dump()) == result
    assert TextEncodingReport.model_validate_json(result.model_dump_json()) == result
    assert set(json.loads(result.model_dump_json())) == {
        "outcome", "source_encoding", "target_encoding", "bom_removed", "issues"
    }


def test_issue_details_are_a_read_only_snapshot_and_serialize_normally():
    details = {"text": "utf-8", "count": 2, "flag": True, "missing": None}
    issue = TextEncodingIssue(code="normalization.text.decode_failed", message="Failed.", detail=details)
    details["count"] = 9
    assert issue.detail["count"] == 2
    assert type(issue.detail["flag"]) is bool
    with pytest.raises(TypeError):
        issue.detail["count"] = 3
    assert json.loads(issue.model_dump_json())["detail"] == dict(issue.detail)


def test_default_issue_details_are_also_read_only():
    issue = TextEncodingIssue(code=TextEncodingFinding.DECODE_FAILED, message="Failed.")
    with pytest.raises(TypeError):
        issue.detail["injected"] = True


@pytest.mark.parametrize("instance,field,value", [
    (_issue(), "message", "Changed."),
    (TextEncodingReport(outcome="changed", source_encoding="cp1252"), "bom_removed", True),
])
def test_results_and_issues_cannot_be_reassigned(instance, field, value):
    with pytest.raises(ValidationError):
        setattr(instance, field, value)


@pytest.mark.parametrize("message", ["", " \t\n"])
def test_blank_issue_messages_are_rejected(message):
    with pytest.raises(ValidationError):
        TextEncodingIssue(code=TextEncodingFinding.DECODE_FAILED, message=message)


@pytest.mark.parametrize("detail", [{"nested": []}, {"nested": {}}, {"object": object()}])
def test_issue_details_do_not_accept_mutable_or_unsupported_values(detail):
    with pytest.raises(ValidationError):
        TextEncodingIssue(code=TextEncodingFinding.DECODE_FAILED, message="Failed.", detail=detail)


@pytest.mark.parametrize("data", [
    {"outcome": "failed"},
    {"outcome": "failed", "issues": (_issue(),), "bom_removed": True},
    {"outcome": "changed"},
    {"outcome": "changed", "source_encoding": "utf-8", "issues": (_issue(),)},
    {"outcome": "no_change"},
    {"outcome": "no_change", "source_encoding": "cp1252"},
    {"outcome": "no_change", "source_encoding": "utf-8", "bom_removed": True},
    {"outcome": "no_change", "source_encoding": "utf-8", "issues": (_issue(),)},
    {"outcome": "changed", "source_encoding": " \t"},
    {"outcome": "unknown", "source_encoding": "utf-8"},
])
def test_contradictory_or_incomplete_results_are_rejected(data):
    with pytest.raises(ValidationError):
        TextEncodingReport(**data)


def test_results_snapshot_an_issue_sequence():
    issues = [_issue()]
    result = TextEncodingReport(outcome="failed", issues=issues)
    issues.clear()
    assert result.issues == (_issue(),)
