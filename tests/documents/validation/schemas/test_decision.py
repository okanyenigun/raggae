from itertools import permutations

from pydantic import ValidationError
import pytest

from raggae.documents.validation import Decision, DecisionPolicy, Severity
from raggae.documents.validation.schemas import decision as decision_module
from raggae.documents.validation.schemas.decision import assemble_default_severities
from tests.documents.validation.checks.test_policies import POLICY_CLASSES


def test_decision_enum_values():
    assert {value.value for value in Decision} == {"accept", "accept_with_warnings", "needs_password", "reject"}


def test_default_severity_assembly_matches_all_check_policies():
    expected = {}
    for policy_class in POLICY_CLASSES:
        for code, severity in policy_class().severities.items():
            assert code not in expected or expected[code] is severity
            expected[code] = severity
    assert assemble_default_severities() == expected
    assert DecisionPolicy().severity_by_code == expected


def test_severity_assembly_and_decision_instances_do_not_share_maps():
    first, second = assemble_default_severities(), assemble_default_severities()
    original = dict(second)
    first["filename.empty"] = Severity.INFO
    assert second == original
    assert assemble_default_severities() == original
    first_policy, second_policy = DecisionPolicy(), DecisionPolicy()
    first_policy.severity_by_code["filename.empty"] = Severity.INFO
    assert second_policy.severity_by_code == original
    assert DecisionPolicy().severity_by_code == original


@pytest.mark.parametrize("second_severity", [Severity.INFO, Severity.WARNING])
def test_severity_assembly_allows_matching_duplicates_and_rejects_conflicts(monkeypatch, second_severity):
    first = decision_module.FilenamePolicy(severities={"custom.shared": Severity.INFO})
    second = decision_module.IdentityPolicy(severities={"custom.shared": second_severity})
    monkeypatch.setattr(decision_module, "FilenamePolicy", lambda: first)
    monkeypatch.setattr(decision_module, "IdentityPolicy", lambda: second)
    if second_severity is Severity.INFO:
        assert assemble_default_severities()["custom.shared"] is Severity.INFO
    else:
        with pytest.raises(ValueError, match="Conflicting severities for 'custom.shared'"):
            assemble_default_severities()


DECISION_CASES = [
    ((), Decision.ACCEPT),
    (("info",), Decision.ACCEPT),
    (("warning",), Decision.ACCEPT_WITH_WARNINGS),
    (("info", "warning", "warning"), Decision.ACCEPT_WITH_WARNINGS),
    (("recoverable",), Decision.NEEDS_PASSWORD),
    (("recoverable", "other_recoverable"), Decision.NEEDS_PASSWORD),
    (("info", "warning", "recoverable"), Decision.NEEDS_PASSWORD),
    (("reject",), Decision.REJECT),
    (("reject", "recoverable"), Decision.REJECT),
    (("info", "warning", "reject"), Decision.REJECT),
    (("reject", "reject"), Decision.REJECT),
]


@pytest.mark.parametrize("codes,expected", DECISION_CASES)
def test_decision_matrix_is_independent_of_order_and_stop_setting(codes, expected, finding_factory):
    for stop in [False, True]:
        policy = DecisionPolicy(
            severity_by_code={"info": "info", "warning": "warning", "reject": "reject",
                              "recoverable": "reject", "other_recoverable": "reject"},
            recoverable_codes={"recoverable", "other_recoverable"},
            stop_on_first_rejection=stop,
        )
        for ordered_codes in set(permutations(codes)):
            findings = tuple(finding_factory(code) for code in ordered_codes)
            assert policy.decide(findings) is expected
            for finding in findings:
                assert policy.rejects(finding) is (policy.severity_of(finding) is Severity.REJECT)


@pytest.mark.parametrize("severity,expected", [
    (Severity.INFO, Decision.ACCEPT),
    (Severity.WARNING, Decision.ACCEPT_WITH_WARNINGS),
    (Severity.REJECT, Decision.REJECT),
])
def test_unknown_code_uses_configured_fallback(severity, expected, finding_factory):
    policy = DecisionPolicy(default_severity=severity)
    finding = finding_factory("future.unknown")
    assert policy.severity_of(finding) is severity
    assert policy.rejects(finding) is (severity is Severity.REJECT)
    assert policy.decide((finding,)) is expected


@pytest.mark.parametrize("code", ["encryption.password_required", "encryption.password_incorrect"])
def test_default_password_rejections_are_recoverable(code, finding_factory):
    policy = DecisionPolicy()
    finding = finding_factory(code)
    assert policy.rejects(finding)
    assert policy.decide((finding,)) is Decision.NEEDS_PASSWORD


def test_password_protected_refusal_is_not_recoverable(finding_factory):
    assert DecisionPolicy().decide((finding_factory("encryption.password_protected"),)) is Decision.REJECT


@pytest.mark.parametrize("severity,expected", [
    ("info", Decision.ACCEPT), ("warning", Decision.ACCEPT_WITH_WARNINGS),
    ("reject", Decision.NEEDS_PASSWORD),
])
def test_recoverability_only_matters_for_rejecting_findings(severity, expected, finding_factory):
    policy = DecisionPolicy(severity_by_code={"custom.recoverable": severity}, recoverable_codes={"custom.recoverable"})
    assert policy.decide((finding_factory("custom.recoverable"),)) is expected


def test_custom_and_empty_recoverable_sets(finding_factory):
    finding = finding_factory("custom.recoverable")
    assert DecisionPolicy(severity_by_code={finding.code: "reject"}, recoverable_codes={finding.code}).decide((finding,)) is Decision.NEEDS_PASSWORD
    assert DecisionPolicy(severity_by_code={finding.code: "reject"}, recoverable_codes=set()).decide((finding,)) is Decision.REJECT


@pytest.mark.parametrize("mapping", [{}, {"custom.only": "info"}])
def test_explicit_severity_map_replaces_defaults(mapping, finding_factory):
    policy = DecisionPolicy(severity_by_code=mapping)
    assert set(policy.severity_by_code) == set(mapping)
    assert policy.severity_of(finding_factory("filename.empty")) is Severity.WARNING


def test_copying_defaults_and_overriding_one_code_preserves_the_rest(finding_factory):
    original = assemble_default_severities()
    copied = {**original, "filename.path_components": Severity.REJECT}
    policy = DecisionPolicy(severity_by_code=copied)
    assert policy.severity_of(finding_factory("filename.path_components")) is Severity.REJECT
    assert policy.severity_of(finding_factory("filename.empty")) is Severity.REJECT
    assert original["filename.path_components"] is Severity.WARNING
    copied["filename.empty"] = Severity.INFO
    assert policy.severity_of(finding_factory("filename.empty")) is Severity.REJECT


@pytest.mark.parametrize("data", [
    {"severity_by_code": None}, {"severity_by_code": {"custom": "fatal"}},
    {"default_severity": "fatal"},
])
def test_invalid_decision_policy_configuration_is_rejected(data):
    with pytest.raises(ValidationError):
        DecisionPolicy(**data)


def test_decision_policy_defaults_frozen_attributes_and_json_round_trip():
    policy = DecisionPolicy()
    assert policy.default_severity is Severity.WARNING
    assert policy.stop_on_first_rejection is True
    assert policy.recoverable_codes == {"encryption.password_required", "encryption.password_incorrect"}
    with pytest.raises(ValidationError) as error:
        policy.default_severity = Severity.INFO
    assert error.value.errors()[0]["type"] == "frozen_instance"
    assert DecisionPolicy.model_validate_json(policy.model_dump_json()) == policy
