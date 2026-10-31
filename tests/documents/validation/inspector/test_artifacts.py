"""The coordinator forwards an artifact's access information to existing checks."""

import pytest

from raggae.documents.normalization import Artifact


@pytest.mark.parametrize("password", [None, "", " correct secret "])
def test_file_artifact_forwards_its_password_to_all_format_checks(inspector_factory, missing_path, password):
    assembly = inspector_factory()
    artifact = Artifact(path=missing_path, password=password)
    report = assembly.inspector.validate(artifact, submitted_filename="uploaded.pdf")
    assert assembly.calls[0][2] == {"filename": "uploaded.pdf"}
    assert all(call[1] == (missing_path, "pdf", password) for call in assembly.calls[3:])
    assert " correct secret " not in repr(report)
    assert artifact.password == password


def test_memory_artifact_is_rejected_without_running_checks(inspector_factory):
    assembly = inspector_factory()
    with pytest.raises(ValueError, match="file-backed"):
        assembly.inspector.validate(Artifact(data=b"pdf", password="secret"))
    assert assembly.calls == []


@pytest.mark.parametrize("password", ["", "different-secret"])
def test_separate_password_cannot_override_an_artifact(inspector_factory, missing_path, password):
    assembly = inspector_factory()
    with pytest.raises(TypeError, match="on the Artifact"):
        assembly.inspector.validate(Artifact(path=missing_path, password="original"), password=password)
    assert assembly.calls == []
