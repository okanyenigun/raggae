from pathlib import Path

import pytest

from raggae.documents.validation import Decision
from raggae.documents.validation.schemas.result import CheckOutcome


@pytest.mark.parametrize("string_path", [False, True])
@pytest.mark.parametrize("submitted", [None, "client.pdf"])
@pytest.mark.parametrize("password", [None, "", "private-password"])
def test_call_order_arguments_and_report(inspector_factory, missing_path, string_path, submitted, password):
    assembly = inspector_factory()
    target = str(missing_path) if string_path else missing_path
    report = assembly.inspector.validate(target, submitted_filename=submitted, password=password)
    names = [f"test_{stage}" for stage in assembly.workers]
    assert [call[0] for call in assembly.calls] == names
    assert assembly.calls[0][1:] == ((), {"filename": submitted if submitted is not None else missing_path.name})
    assert assembly.calls[1][1:] == ((), {"path": missing_path})
    assert assembly.calls[2][1:] == ((), {"path": missing_path, "claimed_extension": "pdf"})
    for _, args, kwargs in assembly.calls[3:]:
        assert args == (missing_path, "pdf", password)
        assert isinstance(args[0], Path)
        assert kwargs == {}
    assert report.decision is Decision.ACCEPT
    assert report.findings == ()
    assert dict(report.facts) == {"claimed_extension": "pdf", "size_bytes": 7, "detected_format": "pdf"}
    assert report.checks_that_ran == tuple(names)
    assert report.checks_that_did_not == ()
    assert all(check.ran is True and check.skipped_because is None for check in report.checks)
    assert "password" not in report.facts


@pytest.mark.parametrize("claimed", [None, "pdf", ".PDF", 7])
def test_claimed_extension_reaches_detector_as_optional_string(inspector_factory, missing_path, claimed):
    assembly = inspector_factory()
    assembly.workers["filename"].outcome = CheckOutcome(facts={"claimed_extension": claimed})
    assembly.inspector.validate(missing_path)
    assert assembly.calls[2][2]["claimed_extension"] == (None if claimed is None else str(claimed))


def test_absent_claimed_extension_does_not_invent_one(inspector_factory, missing_path):
    assembly = inspector_factory()
    assembly.workers["filename"].outcome = CheckOutcome()
    report = assembly.inspector.validate(missing_path)
    assert assembly.calls[2][2]["claimed_extension"] is None
    assert "claimed_extension" not in report.facts
