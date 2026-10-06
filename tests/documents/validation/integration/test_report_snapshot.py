import pytest

from raggae.documents.validation import Decision, DocumentInspector


def test_inspector_returns_an_unchangeable_fact_snapshot(file_factory):
    path = file_factory("document.txt", b"Ordinary document text.")
    inspector = DocumentInspector()
    report = inspector.validate(path)
    snapshot = report.model_dump()
    assert report.decision is Decision.ACCEPT
    with pytest.raises(TypeError):
        report.facts["size_bytes"] = 0
    with pytest.raises(TypeError):
        del report.facts["digest"]
    serialized = report.model_dump()
    serialized["facts"]["size_bytes"] = 0
    assert report.model_dump() == snapshot
    assert inspector.validate(path) == report
