"""Shared models stay independent of any one worker's report schema."""

from dataclasses import FrozenInstanceError, fields, replace

import pytest
from pydantic import BaseModel, ConfigDict

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    StrictTextEncodingNormalizer,
    TextEncodingReport,
)


class AlternativeReport(BaseModel):
    """Test-only stand-in for another worker; not a production abstraction."""

    model_config = ConfigDict(frozen=True)
    outcome: str
    operation: str | None = None


@pytest.mark.parametrize("report", [
    TextEncodingReport(outcome="changed", source_encoding="utf-16-le"),
    AlternativeReport(outcome="changed", operation="rotate"),
])
def test_one_result_accepts_different_worker_reports(report):
    artifact = Artifact(data=b"document")
    result = NormalizationResult(artifact=artifact, report=report)
    assert result.artifact is artifact
    assert result.report is report
    assert result.outcome == "changed"
    assert [field.name for field in fields(result)] == ["artifact", "report"]
    assert not hasattr(result, "source_encoding")


@pytest.mark.parametrize("outcome", ["changed", "no_change"])
def test_success_requires_document_regardless_of_report_type(outcome):
    with pytest.raises(ValueError, match="successful result"):
        NormalizationResult(artifact=None, report=AlternativeReport(outcome=outcome))


def test_failure_requires_no_document_regardless_of_report_type():
    report = AlternativeReport(outcome="failed")
    result = NormalizationResult(artifact=None, report=report)
    assert result.artifact is None
    with pytest.raises(ValueError, match="failed result"):
        NormalizationResult(artifact=Artifact(data=b"partial"), report=report)


@pytest.mark.parametrize("outcome", ["deferred", "unknown", ""])
def test_unknown_outcomes_rejected(outcome):
    with pytest.raises(ValueError, match="supported normalization outcome"):
        NormalizationResult(artifact=None, report=AlternativeReport(outcome=outcome))


def test_report_without_outcome_rejected():
    class ReportWithoutOutcome(BaseModel):
        pass

    with pytest.raises(ValueError, match="supported normalization outcome"):
        NormalizationResult(artifact=None, report=ReportWithoutOutcome())


def test_request_can_be_advanced_by_future_coordinator_without_mutating_original():
    source = Artifact(data=b"original")
    request = NormalizationRequest(
        source=source, output_path=None, declared_encoding="cp1252"
    )
    next_source = Artifact(data=b"next")
    advanced = replace(request, source=next_source)
    assert request.source is source
    assert advanced.source is next_source
    assert advanced.output_path is None
    assert advanced.declared_encoding == "cp1252"


def test_models_are_frozen_and_worker_returns_shared_container():
    request = NormalizationRequest(source=b"hello", output_path=None)
    result = StrictTextEncodingNormalizer().normalize(request)
    assert type(result) is NormalizationResult
    assert isinstance(result.report, TextEncodingReport)
    assert result.artifact is request.source
    assert result.outcome == "no_change"
    with pytest.raises(FrozenInstanceError):
        result.report = AlternativeReport(outcome="failed")
    with pytest.raises(FrozenInstanceError):
        request.source = Artifact(data=b"different")
