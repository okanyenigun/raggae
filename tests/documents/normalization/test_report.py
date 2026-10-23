"""Aggregate reports retain each worker's schema and execution order."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from raggae.documents import normalization
from raggae.documents.normalization import (
    DocumentNormalizationReport,
    DocumentNormalizer,
    HtmlMetadataReport,
    NormalizationStepReport,
    TextEncodingReport,
)
from raggae.documents.normalization.coordinator import DocumentNormalizer as Coordinator
from raggae.documents.normalization.report import (
    DocumentNormalizationReport as ChainReport,
    NormalizationStepReport as StepReport,
)


class CustomReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    operation: str
    details: tuple[str, ...]


def test_public_exports_resolve_to_coordinator_and_report_models():
    assert DocumentNormalizer is Coordinator
    assert DocumentNormalizationReport is ChainReport
    assert NormalizationStepReport is StepReport
    assert {"DocumentNormalizer", "DocumentNormalizationReport", "NormalizationStepReport"} <= set(normalization.__all__)


def test_worker_specific_fields_survive_dict_and_json_serialization():
    encoding = TextEncodingReport(outcome="changed", source_encoding="cp1252")
    metadata = HtmlMetadataReport(outcome="changed", removed_tags=2)
    custom = CustomReport(outcome="no_change", operation="custom", details=("fact",))
    report = DocumentNormalizationReport(
        outcome="changed",
        steps=(
            NormalizationStepReport(worker="encoding", report=encoding),
            NormalizationStepReport(worker="metadata", report=metadata),
            NormalizationStepReport(worker="custom", report=custom),
        ),
    )

    expected = {
        "outcome": "changed",
        "steps": [
            {"worker": "encoding", "report": encoding.model_dump(mode="json"), "output_path": None},
            {"worker": "metadata", "report": metadata.model_dump(mode="json"), "output_path": None},
            {"worker": "custom", "report": custom.model_dump(mode="json"), "output_path": None},
        ],
    }
    assert report.model_dump(mode="json") == expected
    assert json.loads(report.model_dump_json()) == expected
    assert report.steps[0].report is encoding
    assert report.steps[1].report is metadata
    assert report.steps[2].report is custom
    assert report.steps[2].report.operation == "custom"
    assert set(DocumentNormalizationReport.model_fields) == {"outcome", "steps"}
    assert set(NormalizationStepReport.model_fields) == {"worker", "report", "output_path"}


def test_repeated_names_serialize_as_separate_ordered_steps():
    first = HtmlMetadataReport(outcome="changed", removed_tags=1)
    second = HtmlMetadataReport(outcome="no_change")
    report = DocumentNormalizationReport(
        outcome="changed",
        steps=(
            NormalizationStepReport(worker="same", report=first),
            NormalizationStepReport(worker="same", report=second),
        ),
    )
    data = json.loads(report.model_dump_json())
    assert [step["worker"] for step in data["steps"]] == ["same", "same"]
    assert [step["report"]["outcome"] for step in data["steps"]] == ["changed", "no_change"]


def test_report_and_steps_are_frozen():
    step = NormalizationStepReport(worker="metadata", report=HtmlMetadataReport(outcome="no_change"))
    report = DocumentNormalizationReport(outcome="no_change", steps=(step,))
    with pytest.raises(ValidationError, match="frozen"):
        report.outcome = "failed"
    with pytest.raises(ValidationError, match="frozen"):
        report.steps = ()
    with pytest.raises(ValidationError, match="frozen"):
        step.worker = "different"
    with pytest.raises(ValidationError, match="frozen"):
        step.report = HtmlMetadataReport(outcome="changed", removed_tags=1)
    with pytest.raises(ValidationError, match="frozen"):
        step.output_path = Path("changed.html")


def test_saved_step_path_serializes_without_document_bytes(tmp_path):
    path = tmp_path / "saved.html"
    step = NormalizationStepReport(
        worker="metadata", report=HtmlMetadataReport(outcome="changed", removed_tags=1),
        output_path=path,
    )
    assert step.output_path == path
    assert step.model_dump()["output_path"] == path
    assert json.loads(step.model_dump_json())["output_path"] == str(path)


@pytest.mark.parametrize("outcome", ["changed", "no_change", "failed"])
def test_supported_aggregate_outcomes(outcome):
    assert DocumentNormalizationReport(outcome=outcome).outcome == outcome


@pytest.mark.parametrize("outcome", ["unknown", "", None, True])
def test_unsupported_aggregate_outcomes_are_rejected(outcome):
    with pytest.raises(ValidationError):
        DocumentNormalizationReport(outcome=outcome)
