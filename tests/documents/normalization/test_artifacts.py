"""The artifact carries one document copy and its optional access password."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from raggae.documents.normalization import Artifact


@pytest.mark.parametrize("data", [b"", b"hello", b"\xff\x00"])
def test_memory_artifact_contains_immutable_bytes_without_exposing_them_in_repr(data):
    artifact = Artifact(data=data)
    assert artifact.read_bytes() == artifact.data == data
    assert artifact.path is None
    assert "data=" not in repr(artifact)


def test_file_artifact_reads_without_requiring_user_context_management(tmp_path):
    path = tmp_path / "document.txt"
    path.write_bytes(b"hello")
    artifact = Artifact(path=str(path))
    assert artifact.path == path
    assert artifact.data is None
    assert artifact.read_bytes() == b"hello"
    assert path.read_bytes() == b"hello"


@pytest.mark.parametrize("kwargs,error", [
    ({}, ValueError),
    ({"path": "a", "data": b""}, ValueError),
    ({"path": ""}, ValueError),
    ({"path": 1}, TypeError),
    ({"data": "text"}, TypeError),
    ({"data": bytearray(b"text")}, TypeError),
])
def test_artifact_requires_exactly_one_valid_path_or_bytes(kwargs, error):
    with pytest.raises(error):
        Artifact(**kwargs)


def test_artifact_has_no_ownership_or_lifetime_fields():
    assert set(Artifact.__dataclass_fields__) == {"path", "data", "password"}
    assert not hasattr(Artifact, "close")


def test_relative_path_is_resolved_without_opening_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifact = Artifact(path="document.txt")
    assert artifact.path == tmp_path / "document.txt"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("artifact,attribute,value", [
    (Artifact(data=b"a"), "data", b"b"),
    (Artifact(path=Path("a")), "path", Path("b")),
    (Artifact(data=b"a", password="secret"), "password", "changed"),
])
def test_artifact_fields_are_frozen(artifact, attribute, value):
    with pytest.raises(FrozenInstanceError):
        setattr(artifact, attribute, value)


@pytest.mark.parametrize("password", [None, "", " secret with spaces ", "pässword"])
@pytest.mark.parametrize("on_disk", [False, True])
def test_password_belongs_to_copy_is_exact_and_not_printed(password, on_disk, tmp_path):
    artifact = Artifact(path=tmp_path / "document.pdf", password=password) if on_disk else Artifact(data=b"pdf", password=password)
    assert artifact.password == password
    assert "password=" not in repr(artifact)
    if password:
        assert password not in repr(artifact)


@pytest.mark.parametrize("password", [False, 123, b"raw-bytes", bytearray(b"secret"), object()])
def test_password_type_is_checked_at_artifact_boundary(password):
    with pytest.raises(TypeError, match="password"):
        Artifact(path="not-opened.pdf", password=password)
