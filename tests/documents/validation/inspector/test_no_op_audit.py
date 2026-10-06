import pytest

from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe


@pytest.mark.parametrize("dependency,factory", [
    pytest.param("filename_validator", NoOpFilenameValidator, id="filename"),
    pytest.param("identity_probe", NoOpIdentityProbe, id="identity"),
    pytest.param("content_type_detector", NoOpContentTypeDetector, id="content-type"),
])
def test_disabled_no_op_is_not_reported_as_document_inspection(
    inspector_factory, missing_path, dependency, factory,
):
    worker = factory()
    assembly = inspector_factory(**{dependency: worker})
    report = assembly.inspector.validate(missing_path)
    audit = next(check for check in report.checks if check.check == worker.name)

    # CheckRun documents ran as inspection, explicitly including null objects.
    assert audit.ran is False
    assert isinstance(audit.skipped_because, str) and audit.skipped_because.strip()
    assert worker.name in report.checks_that_did_not
    assert worker.name not in report.checks_that_ran
