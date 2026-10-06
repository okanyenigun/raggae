import pytest

from raggae.documents.validation.checks.active_content.no import NoOpActiveContentProbe
from raggae.documents.validation.checks.capability.no import NoOpCapabilityProbe
from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe
from raggae.documents.validation.checks.resource_limits.no import NoOpResourceProbe
from raggae.documents.validation.schemas.result import CheckOutcome


@pytest.mark.parametrize("factory", [
    NoOpFilenameValidator, NoOpIdentityProbe, NoOpContentTypeDetector,
    NoOpResourceProbe, NoOpActiveContentProbe, NoOpCapabilityProbe,
])
def test_no_op_disabled_marker_is_explicit_and_read_only(factory):
    worker = factory()
    assert worker.disabled is True
    with pytest.raises(AttributeError):
        worker.disabled = False
    assert factory().disabled is True


@pytest.mark.parametrize("dependency,factory", [
    ("filename_validator", NoOpFilenameValidator),
    ("identity_probe", NoOpIdentityProbe),
    ("content_type_detector", NoOpContentTypeDetector),
])
def test_disabled_non_format_worker_is_never_called(
    inspector_factory, missing_path, monkeypatch, forbidden_operation, dependency, factory,
):
    worker = factory()
    monkeypatch.setattr(worker, "check", forbidden_operation)
    report = inspector_factory(**{dependency: worker}).inspector.validate(missing_path)
    audit = next(check for check in report.checks if check.check == worker.name)
    assert audit.ran is False
    assert audit.skipped_because == "check disabled"


@pytest.mark.parametrize("stage", ["filename", "identity", "content_type"])
@pytest.mark.parametrize("marker", ["absent", False])
def test_empty_outcome_from_enabled_worker_is_still_an_inspection(
    inspector_factory, missing_path, stage, marker,
):
    assembly = inspector_factory()
    worker = assembly.workers[stage]
    worker.outcome = CheckOutcome()
    if marker != "absent":
        worker.disabled = marker
    report = assembly.inspector.validate(missing_path)
    audit = next(check for check in report.checks if check.check == worker.name)
    assert audit.ran is True
    assert audit.skipped_because is None
    assert worker.name in report.checks_that_ran
    assert sum(call[0] == worker.name for call in assembly.calls) == 1
