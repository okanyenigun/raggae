from pathlib import Path

import pytest

from raggae.documents.validation import Decision, DocumentValidationReport
from raggae.documents.validation.checks.active_content.no import NoOpActiveContentProbe
from raggae.documents.validation.checks.capability.no import NoOpCapabilityProbe
from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe
from raggae.documents.validation.checks.resource_limits.no import NoOpResourceProbe


SUBSTITUTIONS = [
    pytest.param("filename_validator", NoOpFilenameValidator, "filename", False, id="filename"),
    pytest.param("identity_probe", NoOpIdentityProbe, "identity", False, id="identity"),
    pytest.param("content_type_detector", NoOpContentTypeDetector, "content_type", False, id="content-type"),
    pytest.param("resource_probes", NoOpResourceProbe, "resource", True, id="resource"),
    pytest.param("active_content_probes", NoOpActiveContentProbe, "active_content", True, id="active-content"),
    pytest.param("capability_probes", NoOpCapabilityProbe, "capability", False, id="capability"),
]


@pytest.mark.parametrize("dependency,factory,stage,group", SUBSTITUTIONS)
def test_no_op_replaces_dependency_without_io_or_invented_facts(
    inspector_factory, missing_path, monkeypatch, forbidden_operation,
    dependency, factory, stage, group,
):
    worker = factory()
    assembly = inspector_factory(**{dependency: (worker,) if group else worker})
    with monkeypatch.context() as guard:
        for method in ("open", "read_text", "read_bytes", "stat", "lstat"):
            guard.setattr(Path, method, forbidden_operation)
        report = assembly.inspector.validate(missing_path, password="private-password")

    assert isinstance(report, DocumentValidationReport)
    assert report.decision is Decision.ACCEPT
    assert report.findings == ()
    expected_facts = {"claimed_extension": "pdf", "size_bytes": 7, "detected_format": "pdf"}
    if stage in ("filename", "identity", "content_type"):
        expected_facts.pop({
            "filename": "claimed_extension", "identity": "size_bytes", "content_type": "detected_format",
        }[stage])
    assert dict(report.facts) == expected_facts

    expected_calls = [name for name in assembly.workers if name != stage]
    if stage == "content_type":
        expected_calls = ["filename", "identity"]  # No detected format to route.
    assert [call[0] for call in assembly.calls] == [f"test_{name}" for name in expected_calls]
    assert len(report.checks) == 7
    if stage in ("filename", "identity", "content_type"):
        assert sum(check.check == worker.name for check in report.checks) == 1
    else:
        label = {"resource": "resource limits", "active_content": "active content", "capability": "capability"}[stage]
        skipped = next(check for check in report.checks if check.check == label)
        assert skipped.ran is False
        assert skipped.skipped_because == "no probe covers 'pdf'"
