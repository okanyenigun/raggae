from types import SimpleNamespace
import zipfile

import pytest

from raggae.documents.validation import ArchiveLimits, ArchiveResourceProbe
from raggae.documents.validation.checks.resource_limits import archieve as archive_module
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code


class ArchiveDocument:
    def __init__(self, sizes):
        self.entries = [SimpleNamespace(file_size=expanded, compress_size=compressed)
                        for expanded, compressed in sizes]
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def infolist(self):
        return self.entries


def use_archive(monkeypatch, sizes):
    document = ArchiveDocument(sizes)
    monkeypatch.setattr(archive_module.zipfile, "ZipFile", lambda *a, **k: document)
    return document


@pytest.mark.parametrize("format_name", [None, "docx", "xlsx", "pptx", " .DOCX "])
@pytest.mark.parametrize("as_string", [False, True])
def test_real_supported_formats_and_exact_facts(archive_factory, format_name, as_string):
    path = archive_factory(entries=[("part.xml", b"hello"), ("second.xml", b"abc")])
    before = path.read_bytes()
    outcome = ArchiveResourceProbe().check(str(path) if as_string else path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"archive_entries": 2, "uncompressed_bytes": 8, "expansion_ratio": 1.0}
    assert path.read_bytes() == before


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("password", [None, "", "irrelevant password"])
def test_real_compression_and_irrelevant_password(archive_factory, compression, password):
    path = archive_factory(entries=[("a.xml", b"A" * 256), ("b.xml", b"second part")], compression=compression)
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        expanded = sum(e.file_size for e in entries)
        compressed = sum(e.compress_size for e in entries)
    outcome = ArchiveResourceProbe().check(path, password=password)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"archive_entries": 2, "uncompressed_bytes": expanded,
                                 "expansion_ratio": round(expanded / compressed, 2)}


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_real_entry_count_boundary(archive_factory, count):
    path = archive_factory(entries=[(f"{i}.xml", b"x") for i in range(count)])
    outcome = ArchiveResourceProbe(ArchiveLimits(max_entries=2)).check(path)
    assert outcome.codes == ((Code.TOO_MANY_ENTRIES,) if count > 2 else ())
    assert outcome.facts["archive_entries"] == count
    assert outcome.facts["uncompressed_bytes"] == count
    assert outcome.facts["expansion_ratio"] == (1.0 if count else 0.0)
    if count > 2:
        assert dict(outcome.findings[0].detail) == {"limit": 2, "observed": 3}


@pytest.mark.parametrize("size", [9, 10, 11])
def test_real_expanded_byte_boundary(archive_factory, size):
    path = archive_factory(entries=[("part.xml", b"x" * size)])
    outcome = ArchiveResourceProbe(ArchiveLimits(max_uncompressed_bytes=10)).check(path)
    assert outcome.codes == ((Code.EXPANDS_TOO_LARGE,) if size > 10 else ())
    assert outcome.facts["uncompressed_bytes"] == size
    if size > 10:
        assert dict(outcome.findings[0].detail) == {"limit": 10, "observed": 11}


@pytest.mark.parametrize("expanded", [1999, 2000, 2001])
def test_ratio_uses_unrounded_value_at_boundary(monkeypatch, missing_path, expanded):
    document = use_archive(monkeypatch, [(expanded, 1000)])
    outcome = ArchiveResourceProbe(ArchiveLimits(max_expansion_ratio=2)).check(missing_path)
    assert outcome.codes == ((Code.EXPANSION_RATIO_TOO_HIGH,) if expanded > 2000 else ())
    assert outcome.facts["expansion_ratio"] == 2.0
    if expanded > 2000:
        assert dict(outcome.findings[0].detail) == {"limit": 2, "observed": 2.0}
    assert document.closed


def test_ratio_uses_totals_not_largest_member(monkeypatch, missing_path):
    document = use_archive(monkeypatch, [(100, 1), (1, 100)])
    outcome = ArchiveResourceProbe(ArchiveLimits(max_expansion_ratio=2)).check(missing_path)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"archive_entries": 2, "uncompressed_bytes": 101, "expansion_ratio": 1.0}
    assert document.closed


def test_all_three_exceeded_limits_are_reported(monkeypatch, missing_path):
    document = use_archive(monkeypatch, [(10, 1), (10, 1), (10, 1)])
    outcome = ArchiveResourceProbe(ArchiveLimits(max_entries=2, max_uncompressed_bytes=20,
                                                max_expansion_ratio=5)).check(missing_path)
    assert outcome.codes == (Code.TOO_MANY_ENTRIES, Code.EXPANDS_TOO_LARGE, Code.EXPANSION_RATIO_TOO_HIGH)
    assert [dict(f.detail) for f in outcome.findings] == [{"limit": 2, "observed": 3},
                                                        {"limit": 20, "observed": 30},
                                                        {"limit": 5, "observed": 10.0}]
    assert dict(outcome.facts) == {"archive_entries": 3, "uncompressed_bytes": 30, "expansion_ratio": 10.0}
    assert document.closed


def test_real_directory_and_nested_archive_are_only_metadata_entries(archive_factory, monkeypatch, forbidden_operation):
    path = archive_factory(entries=[("folder/", b""), ("folder/part.xml", b"abc"),
                                    ("nested.zip", b"not opened recursively")])
    with zipfile.ZipFile(path) as archive:
        expected_size = sum(e.file_size for e in archive.infolist())
    for method in ("open", "read", "extract", "extractall"):
        monkeypatch.setattr(zipfile.ZipFile, method, forbidden_operation)
    outcome = ArchiveResourceProbe().check(path)
    assert outcome.codes == ()
    assert outcome.facts["archive_entries"] == 3
    assert outcome.facts["uncompressed_bytes"] == expected_size


def test_real_duplicate_names_are_counted(archive_factory):
    with pytest.warns(UserWarning, match="Duplicate name"):
        path = archive_factory(entries=[("same.xml", b"abc"), ("same.xml", b"defg")])
    outcome = ArchiveResourceProbe().check(path)
    assert outcome.codes == ()
    assert outcome.facts["archive_entries"] == 2
    assert outcome.facts["uncompressed_bytes"] == 7


def test_corrupt_payload_with_readable_directory_is_not_integrity_validation(archive_factory):
    path = archive_factory(entries=[("part.xml", b"hello")])
    content = bytearray(path.read_bytes())
    # A stored member's payload starts after the fixed local header and ASCII name.
    content[30 + len("part.xml")] ^= 1
    path.write_bytes(content)
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(zipfile.BadZipFile, match="CRC"):
            archive.read("part.xml")
    before = path.read_bytes()
    assert ArchiveResourceProbe().check(path).codes == ()
    assert path.read_bytes() == before


@pytest.mark.parametrize("format_name,normalized", [("zip", "zip"), (" .PDF ", "pdf"), ("jpg", "jpeg"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(archive_module.zipfile, "ZipFile", forbidden_operation)
    outcome = ArchiveResourceProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.docx", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        ArchiveResourceProbe().check(bad_path)


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_and_corrupt_archives_are_unreadable(file_factory, missing_path, kind):
    path = missing_path if kind == "missing" else file_factory("broken.docx", b"not a zip")
    outcome = ArchiveResourceProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


@pytest.mark.parametrize("error_type", [OSError, PermissionError, zipfile.BadZipFile])
@pytest.mark.parametrize("stage", ["open", "directory"])
def test_archive_read_errors_return_unreadable_and_close(monkeypatch, missing_path, error_type, stage):
    document = ArchiveDocument([])

    def fail(*args, **kwargs):
        raise error_type("central directory could not be read")

    if stage == "directory":
        document.infolist = fail
    monkeypatch.setattr(archive_module.zipfile, "ZipFile", fail if stage == "open" else lambda *a, **k: document)
    outcome = ArchiveResourceProbe().check(missing_path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert document.closed == (stage == "directory")


def test_repeated_calls_and_policy_are_instance_local(archive_factory):
    policy = ArchiveLimits(max_entries=1)
    probe = ArchiveResourceProbe(policy)
    large = archive_factory("large.docx", entries=[("a", b"a"), ("b", b"b")])
    small = archive_factory("small.docx", entries=[("a", b"a")])
    assert probe.check(large).codes == (Code.TOO_MANY_ENTRIES,)
    assert probe.check(small).codes == ()
    assert probe.check(large).facts["archive_entries"] == 2
    assert ArchiveResourceProbe().check(large).codes == ()
    assert probe.policy is policy
    assert probe.name == "validation_resource_limits_archive"
    assert probe.handles == frozenset({"docx", "xlsx", "pptx"})
