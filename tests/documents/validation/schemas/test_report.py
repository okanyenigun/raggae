from pydantic import ValidationError
import pytest

from raggae.documents.validation import Decision, DocumentValidationReport, Severity
from raggae.documents.validation.schemas.result import CheckRun, WeightedFinding


@pytest.mark.parametrize("decision,accepted", [
    (Decision.ACCEPT, True), (Decision.ACCEPT_WITH_WARNINGS, True),
    (Decision.NEEDS_PASSWORD, False), (Decision.REJECT, False),
])
def test_accepted_property_and_empty_report_defaults(decision, accepted):
    report = DocumentValidationReport(decision=decision)
    assert report.accepted is accepted
    assert report.findings == report.checks == ()
    assert dict(report.facts) == {}
    assert report.rejecting_findings == ()
    assert report.checks_that_ran == report.checks_that_did_not == ()


def test_report_filters_preserve_order_and_duplicates(finding_factory):
    info = WeightedFinding(finding=finding_factory("info"), severity=Severity.INFO)
    warning = WeightedFinding(finding=finding_factory("warning"), severity=Severity.WARNING)
    reject = WeightedFinding(finding=finding_factory("reject"), severity=Severity.REJECT)
    findings = (info, reject, warning, reject)
    checks = (
        CheckRun(check="first", ran=True),
        CheckRun(check="skipped", ran=False, skipped_because="no probe configured"),
        CheckRun(check="first", ran=True),
        CheckRun(check="unknown", ran=False, skipped_because="format not determined"),
    )
    report = DocumentValidationReport(decision=Decision.REJECT, findings=findings, checks=checks)
    assert report.findings == findings
    assert report.checks == checks
    assert report.rejecting_findings == (reject, reject)
    assert report.checks_that_ran == ("first", "first")
    assert report.checks_that_did_not == ("skipped", "unknown")


def test_report_round_trips_nested_findings_facts_and_audit(finding_factory, scalar_facts):
    report = DocumentValidationReport(
        decision=Decision.ACCEPT_WITH_WARNINGS,
        findings=(WeightedFinding(finding=finding_factory(limit=5, observed=6), severity="warning"),),
        facts=scalar_facts,
        checks=(CheckRun(check="worker", ran=True), CheckRun(check="skip", ran=False, skipped_because="no probe configured")),
    )
    assert DocumentValidationReport.model_validate(report.model_dump()) == report
    assert DocumentValidationReport.model_validate_json(report.model_dump_json()) == report
    for key, value in scalar_facts.items():
        assert type(report.facts[key]) is type(value)


@pytest.mark.parametrize("field,value", [("decision", Decision.REJECT), ("facts", {}), ("checks", ())])
def test_report_attribute_reassignment_is_rejected(field, value):
    with pytest.raises(ValidationError) as error:
        setattr(DocumentValidationReport(decision="accept"), field, value)
    assert error.value.errors()[0]["type"] == "frozen_instance"


def test_report_default_fact_mappings_are_not_shared():
    first, second = DocumentValidationReport(decision="accept"), DocumentValidationReport(decision="accept")
    assert first.facts is not second.facts
    assert dict(first.facts) == dict(second.facts) == {}


@pytest.mark.parametrize("data", [
    {"decision": "unknown"}, {"decision": "accept", "facts": {"nested": {"count": 1}}},
])
def test_report_rejects_invalid_decision_or_nested_fact_values(data):
    with pytest.raises(ValidationError):
        DocumentValidationReport(**data)
