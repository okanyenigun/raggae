from types import MappingProxyType

import pytest

from raggae.documents.validation import Decision, DocumentValidationReport


@pytest.mark.parametrize("kwargs", [
    pytest.param({}, id="omitted"),
    pytest.param({"facts": {}}, id="explicit-empty"),
    pytest.param({"facts": {"page_count": 2}}, id="dictionary"),
    pytest.param({"facts": MappingProxyType({"page_count": 2})}, id="readonly-input"),
])
def test_report_facts_reject_assignment_for_defaults_and_supplied_mappings(kwargs):
    report = DocumentValidationReport(decision=Decision.ACCEPT, **kwargs)
    before = dict(report.facts)
    with pytest.raises(TypeError):
        report.facts["page_count"] = 999
    with pytest.raises(TypeError):
        report.facts["injected"] = True
    assert dict(report.facts) == before


@pytest.mark.parametrize("readonly_input", [False, True])
def test_report_facts_reject_deletion(readonly_input):
    facts = {"page_count": 2}
    report = DocumentValidationReport(decision="accept", facts=MappingProxyType(facts) if readonly_input else facts)
    with pytest.raises(TypeError):
        del report.facts["page_count"]
    assert report.facts["page_count"] == 2


@pytest.mark.parametrize("readonly_input", [False, True])
def test_report_snapshot_is_independent_of_input_mapping(readonly_input):
    original = {"page_count": 2, "has_extractable_text": True}
    report = DocumentValidationReport(decision="accept", facts=MappingProxyType(original) if readonly_input else original)
    original["page_count"] = 999
    del original["has_extractable_text"]
    original["injected"] = "later"
    assert dict(report.facts) == {"page_count": 2, "has_extractable_text": True}


@pytest.mark.parametrize("construction", ["omitted", "empty", "supplied"])
@pytest.mark.parametrize("round_trip", ["python", "json-dict", "json-text"])
def test_report_round_trip_preserves_read_only_facts_and_scalar_types(scalar_facts, construction, round_trip):
    kwargs = {} if construction == "omitted" else {"facts": {} if construction == "empty" else scalar_facts}
    report = DocumentValidationReport(decision=Decision.ACCEPT_WITH_WARNINGS, **kwargs)
    if round_trip == "json-text":
        restored = DocumentValidationReport.model_validate_json(report.model_dump_json())
    else:
        data = report.model_dump(mode="json" if round_trip == "json-dict" else "python")
        assert isinstance(data["facts"], dict)
        restored = DocumentValidationReport.model_validate(data)
    assert restored == report
    assert restored.facts is not report.facts
    for key, value in report.facts.items():
        assert type(restored.facts[key]) is type(value)
    with pytest.raises(TypeError):
        restored.facts["injected"] = True


@pytest.mark.parametrize("mode", ["python", "json"])
def test_serialized_facts_are_independent_mutable_dictionaries(mode):
    report = DocumentValidationReport(decision="accept", facts={"page_count": 2})
    serialized = report.model_dump(mode=mode)
    assert isinstance(serialized["facts"], dict)
    serialized["facts"]["page_count"] = 999
    serialized["facts"]["injected"] = True
    assert dict(report.facts) == {"page_count": 2}
