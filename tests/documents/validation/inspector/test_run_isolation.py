from raggae.documents.validation import Decision, DecisionPolicy, Severity
from raggae.documents.validation.schemas.result import CheckOutcome, Finding


def test_reuse_for_clean_reject_password_clean_does_not_leak_state(inspector_factory, missing_path):
    policy = DecisionPolicy()
    policy.severity_by_code["test.reject"] = Severity.REJECT
    assembly = inspector_factory(policy=policy)
    defaults = {stage: dict(worker.outcome.facts) for stage, worker in assembly.workers.items()}
    saved = []
    for index, (stage, code, expected, password) in enumerate([
        (None, None, Decision.ACCEPT, None),
        ("filename", "test.reject", Decision.REJECT, "rejected-secret"),
        ("encryption", "encryption.password_required", Decision.NEEDS_PASSWORD, "locked-secret"),
        (None, None, Decision.ACCEPT, "clean-secret"),
    ]):
        assembly.calls.clear()
        for name, worker in assembly.workers.items():
            worker.outcome = CheckOutcome(facts=defaults[name])
        filename = assembly.workers["filename"]
        filename.outcome = CheckOutcome(facts={**filename.outcome.facts, "run_token": index})
        if stage is not None:
            worker = assembly.workers[stage]
            worker.outcome = CheckOutcome(
                findings=(Finding(code=code, message="Controlled issue."),), facts=worker.outcome.facts,
            )
        report = assembly.inspector.validate(missing_path, password=password)
        assert report.decision is expected
        assert tuple(weighted.finding.code for weighted in report.findings) == (() if code is None else (code,))
        assert report.facts["run_token"] == index
        assert len(assembly.calls) == {Decision.REJECT: 1, Decision.NEEDS_PASSWORD: 4}.get(expected, 7)
        assert "password" not in report.facts
        assert all(call[1][2] == password for call in assembly.calls[3:])
        saved.append(report)
    assert [report.facts["run_token"] for report in saved] == list(range(4))
    assert saved[0].findings == saved[-1].findings == ()
    assert saved[0].facts is not saved[-1].facts


def test_interleaved_nested_run_has_its_own_facts_findings_and_stop_flag(inspector_factory, missing_path, monkeypatch):
    assembly = inspector_factory()
    inner_path = missing_path.with_name("inner.pdf")
    inner_reports = []
    original_check = assembly.workers["identity"].check

    def inspect_identity(*, path):
        original_check(path=path)
        if path == missing_path:
            inner_reports.append(assembly.inspector.validate(inner_path))
            return CheckOutcome(facts={"identity_tag": "outer"})
        return CheckOutcome(
            findings=(Finding(code="filename.empty", message="Controlled inner rejection."),),
            facts={"identity_tag": "inner"},
        )

    monkeypatch.setattr(assembly.workers["identity"], "check", inspect_identity)
    outer = assembly.inspector.validate(missing_path)
    assert outer.decision is Decision.ACCEPT
    assert outer.findings == ()
    assert outer.facts["identity_tag"] == "outer"
    assert len(outer.checks_that_ran) == 7
    assert len(inner_reports) == 1
    inner = inner_reports[0]
    assert inner.decision is Decision.REJECT
    assert inner.facts["identity_tag"] == "inner"
    assert inner.checks_that_ran == ("test_filename", "test_identity")
