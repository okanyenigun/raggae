from types import SimpleNamespace

import pytest

from raggae.documents.validation.schemas.result import CheckOutcome


FORMAT_STAGES = ("encryption", "resource", "active_content", "capability")
LABELS = ("encryption", "resource limits", "active content", "capability")


@pytest.mark.parametrize("detected,canonical", [
    ("pdf", "pdf"), (" .PDF ", "pdf"), ("jpg", "jpeg"),
    (".JPEG", "jpeg"), ("tif", "tiff"), (" .TIFF ", "tiff"),
])
def test_detected_format_is_normalized_before_worker_calls(inspector_factory, missing_path, detected, canonical):
    assembly = inspector_factory()
    assembly.workers["content_type"].outcome = CheckOutcome(facts={"detected_format": detected})
    for stage in FORMAT_STAGES:
        assembly.workers[stage].handles = frozenset({canonical})
    report = assembly.inspector.validate(missing_path, password="secret")
    assert [call[0] for call in assembly.calls] == [f"test_{stage}" for stage in assembly.workers]
    assert all(call[1] == (missing_path, canonical, "secret") for call in assembly.calls[3:])
    assert report.facts["detected_format"] == detected  # Preserve the worker's fact.


@pytest.mark.parametrize("claimed", ["txt", "docx"])
def test_detected_not_claimed_format_selects_workers(inspector_factory, missing_path, claimed):
    assembly = inspector_factory()
    assembly.workers["filename"].outcome = CheckOutcome(facts={"claimed_extension": claimed})
    report = assembly.inspector.validate(missing_path, submitted_filename=f"claim.{claimed}")
    assert assembly.calls[2][2]["claimed_extension"] == claimed
    assert all(call[1][1] == "pdf" for call in assembly.calls[3:])
    assert len(report.checks_that_ran) == 7


@pytest.mark.parametrize("explicit_null", [False, True])
def test_unknown_format_skips_all_format_workers(inspector_factory, missing_path, explicit_null):
    assembly = inspector_factory()
    assembly.workers["content_type"].outcome = CheckOutcome(facts={"detected_format": None} if explicit_null else {})
    report = assembly.inspector.validate(missing_path)
    assert [call[0] for call in assembly.calls] == ["test_filename", "test_identity", "test_content_type"]
    assert tuple(check.check for check in report.checks[3:]) == LABELS
    assert all(check.ran is False and check.skipped_because == "format not determined" for check in report.checks[3:])


@pytest.mark.parametrize("dependency,stage,label", [
    ("encryption_probe", "encryption", "encryption"),
    ("capability_probes", "capability", "capability"),
])
def test_nonmatching_single_worker_is_not_called(inspector_factory, missing_path, dependency, stage, label):
    assembly = inspector_factory()
    assembly.workers[stage].handles = frozenset({"html"})
    report = assembly.inspector.validate(missing_path)
    assert f"test_{stage}" not in [call[0] for call in assembly.calls]
    audit = next(check for check in report.checks if check.check == label)
    assert audit.ran is False
    assert audit.skipped_because == "no probe covers 'pdf'"


@pytest.mark.parametrize("dependency,stage,label", [
    ("resource_probes", "resource", "resource limits"),
    ("active_content_probes", "active_content", "active content"),
])
@pytest.mark.parametrize("unknown", [False, True])
def test_unconfigured_group_skip_has_priority_over_unknown_format(
    inspector_factory, missing_path, dependency, stage, label, unknown,
):
    assembly = inspector_factory(**{dependency: ()})
    if unknown:
        assembly.workers["content_type"].outcome = CheckOutcome()
    report = assembly.inspector.validate(missing_path)
    assert f"test_{stage}" not in [call[0] for call in assembly.calls]
    audit = next(check for check in report.checks if check.check == label)
    assert audit.ran is False
    assert audit.skipped_because == "no probe configured"


@pytest.mark.parametrize("dependency", ["resource_probes", "active_content_probes"])
@pytest.mark.parametrize("reverse", [False, True])
def test_first_matching_alternative_wins_after_nonmatching_worker(
    inspector_factory, missing_path, dependency, reverse,
):
    selected = []

    def alternative(name, handles):
        def check(*args, **kwargs):
            selected.append((name, args, kwargs))
            return CheckOutcome(facts={"chosen_alternative": name})
        return SimpleNamespace(name=name, handles=frozenset(handles), check=check)

    nonmatching = alternative("unrelated", {"html"})
    matches = [alternative("first", {"pdf"}), alternative("second", {"pdf"})]
    if reverse:
        matches.reverse()
    assembly = inspector_factory(**{dependency: (nonmatching, *matches)})
    report = assembly.inspector.validate(missing_path, password="secret")
    assert selected == [(matches[0].name, (missing_path, "pdf", "secret"), {})]
    assert report.facts["chosen_alternative"] == matches[0].name
    assert matches[0].name in report.checks_that_ran
    assert matches[1].name not in report.checks_that_ran
    assert "unrelated" not in report.checks_that_ran


def test_uncovered_groups_never_call_nonmatching_workers(inspector_factory, missing_path):
    assembly = inspector_factory()
    for stage in FORMAT_STAGES:
        assembly.workers[stage].handles = frozenset({"html"})
    report = assembly.inspector.validate(missing_path)
    assert len(assembly.calls) == 3
    assert tuple(check.check for check in report.checks[3:]) == LABELS
    assert all(check.ran is False and check.skipped_because == "no probe covers 'pdf'" for check in report.checks[3:])
