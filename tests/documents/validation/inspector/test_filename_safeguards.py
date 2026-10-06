import pytest

from raggae.documents.validation import Decision, DecisionPolicy, StrictFilenameValidator


@pytest.mark.parametrize("string_path", [False, True])
@pytest.mark.parametrize("stop", [False, True])
def test_real_filename_worker_rejects_explicit_empty_name(
    inspector_factory, missing_path, string_path, stop,
):
    filename = StrictFilenameValidator()
    assembly = inspector_factory(
        filename_validator=filename, policy=DecisionPolicy(stop_on_first_rejection=stop),
    )
    target = str(missing_path) if string_path else missing_path
    report = assembly.inspector.validate(target, submitted_filename="")
    assert report.decision is Decision.REJECT
    assert tuple(weighted.finding.code for weighted in report.findings) == ("filename.empty",)
    assert "canonical_filename" not in report.facts
    assert "claimed_extension" not in report.facts
    assert report.checks[0].check == filename.name
    assert report.checks[0].ran is True
    if stop:
        assert assembly.calls == []
        assert len(report.checks) == 1  # Later halted stages are not yet audited.
    else:
        assert len(assembly.calls) == 6
        assert assembly.calls[1][2]["claimed_extension"] is None


@pytest.mark.parametrize("string_path", [False, True])
def test_none_still_uses_disk_basename(inspector_factory, missing_path, string_path):
    assembly = inspector_factory()
    assembly.inspector.validate(str(missing_path) if string_path else missing_path, submitted_filename=None)
    assert assembly.calls[0] == ("test_filename", (), {"filename": missing_path.name})


@pytest.mark.parametrize("bad_name", [0, False, b"", []])
def test_invalid_falsey_name_is_not_silently_replaced(inspector_factory, missing_path, bad_name):
    assembly = inspector_factory(filename_validator=StrictFilenameValidator())
    with pytest.raises(TypeError, match="submitted_filename must be a string"):
        assembly.inspector.validate(missing_path, submitted_filename=bad_name)
    assert assembly.calls == []
