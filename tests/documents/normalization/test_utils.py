"""Shared helpers preserve normalization's existing boundary and I/O behavior."""

from io import BytesIO
from pathlib import Path

import pytest

from raggae.documents.normalization.utils import (
    ooxml_relationship_part,
    require_executable,
    require_path,
    require_timeout_seconds,
    resolve_ooxml_target,
    write_all_bytes,
)


@pytest.mark.parametrize("value", ["document.txt", Path("document.txt")])
def test_relative_path_becomes_absolute_without_accessing_disk(
    value, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert require_path(value) == tmp_path / "document.txt"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("as_string", [False, True])
def test_absolute_path_is_preserved(as_string, tmp_path):
    path = tmp_path / "missing.txt"
    assert require_path(str(path) if as_string else path) == path
    assert not path.exists()


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_blank_path_rejected(value):
    with pytest.raises(ValueError, match="path cannot be blank"):
        require_path(value)


@pytest.mark.parametrize("value", [None, 1, False, b"document.txt", object()])
def test_unsupported_types_rejected(value):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        require_path(value)


def test_nonblank_path_whitespace_is_not_stripped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert require_path(" document.txt ") == tmp_path / " document.txt "


@pytest.mark.parametrize("chunk_size", [1, 2, 100])
def test_write_all_bytes_handles_partial_writes(chunk_size):
    class PartialWriter(BytesIO):
        def write(self, data):
            return super().write(data[:chunk_size])

    with PartialWriter() as output:
        write_all_bytes(output, b"complete output")
        assert output.getvalue() == b"complete output"


def test_write_all_bytes_does_not_write_empty_data():
    class UnexpectedWriter:
        def write(self, data):
            raise AssertionError("nothing needs writing")

    write_all_bytes(UnexpectedWriter(), b"")


@pytest.mark.parametrize("count", [None, 0, -1, True, "1", 1.0, 999])
def test_write_all_bytes_rejects_invalid_counts(count):
    class InvalidWriter:
        def write(self, data):
            return count

    with pytest.raises(OSError, match="output did not accept a valid number of bytes"):
        write_all_bytes(InvalidWriter(), b"abc")


@pytest.mark.parametrize("count", [0, 3])
def test_write_all_bytes_checks_remaining_count_after_partial_progress(count):
    class InvalidAfterProgress(BytesIO):
        def write(self, data):
            if self.tell() == 0:
                return super().write(data[:2])
            return count

    with InvalidAfterProgress() as output:
        with pytest.raises(OSError):
            write_all_bytes(output, b"abcd")
        assert output.getvalue() == b"ab"


def test_write_all_bytes_preserves_custom_error_message():
    class InvalidWriter:
        def write(self, data):
            return 0

    with pytest.raises(OSError, match="original message"):
        write_all_bytes(InvalidWriter(), b"abc", error_message="original message")


@pytest.mark.parametrize("error", [OSError("disk full"), UnicodeError("writer error")])
def test_write_all_bytes_propagates_writer_errors_unchanged(error):
    class BrokenWriter:
        def write(self, data):
            raise error

    with pytest.raises(type(error)) as caught:
        write_all_bytes(BrokenWriter(), b"abc")
    assert caught.value is error


@pytest.mark.parametrize("relationship,target,expected", [
    ("word/_rels/document.xml.rels", "vbaProject.bin", "word/vbaProject.bin"),
    ("word/_rels/document.xml.rels", "../docProps/core.xml", "docProps/core.xml"),
    ("word/_rels/document.xml.rels", "/docProps/../docProps/core.xml", "docProps/core.xml"),
    ("word/_rels/document.xml.rels", "..\\docProps\\core.xml", "docProps/core.xml"),
    ("word/_rels/document.xml.rels", "../docProps/core%2Exml?query=1#fragment", "docProps/core.xml"),
    ("_rels/.rels", "docProps/core.xml", "docProps/core.xml"),
    ("word/_rels/document.xml.rels", "", "word"),
    ("word/_rels/document.xml.rels", "#bookmark", "word"),
])
def test_resolve_ooxml_target_preserves_package_resolution_rules(
    relationship, target, expected
):
    assert resolve_ooxml_target(relationship, target) == expected


@pytest.mark.parametrize("part,expected", [
    ("word/vbaProject.bin", "word/_rels/vbaProject.bin.rels"),
    ("docProps/core.xml", "docProps/_rels/core.xml.rels"),
    ("root.xml", "_rels/root.xml.rels"),
])
def test_ooxml_relationship_part(part, expected):
    assert ooxml_relationship_part(part) == expected


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("pandoc", "pandoc"),
    (Path("tools/pandoc"), str(Path("tools/pandoc"))),
    (" tools/pandoc ", " tools/pandoc "),
])
def test_require_executable_preserves_optional_value_without_resolving_it(
    value, expected
):
    assert require_executable(value) == expected


@pytest.mark.parametrize("value", [1, False, b"pandoc", object()])
def test_require_executable_rejects_unsupported_types(value):
    with pytest.raises(TypeError, match="executable must be a string, Path, or None"):
        require_executable(value)


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_require_executable_rejects_blank_strings(value):
    with pytest.raises(ValueError, match="executable cannot be blank"):
        require_executable(value)


@pytest.mark.parametrize("value", [1, 120])
def test_require_timeout_seconds_accepts_positive_integers(value):
    assert require_timeout_seconds(value) == value


@pytest.mark.parametrize("value", [None, True, False, 1.0, "120"])
def test_require_timeout_seconds_rejects_non_integer_values(value):
    with pytest.raises(TypeError, match="timeout_seconds must be an integer"):
        require_timeout_seconds(value)


@pytest.mark.parametrize("value", [0, -1])
def test_require_timeout_seconds_rejects_non_positive_values(value):
    with pytest.raises(ValueError, match="timeout_seconds must be greater than zero"):
        require_timeout_seconds(value)
