import struct
import zipfile

import pytest

from raggae.documents.validation import ArchiveLimits, ArchiveResourceProbe
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code
from .test_archive import use_archive


@pytest.mark.parametrize("format_name", [None, "docx", "xlsx", "pptx"])
def test_positive_expanded_size_without_compressed_bytes_is_unreadable(
    archive_factory, monkeypatch, forbidden_operation, format_name
):
    path = archive_factory(entries=[("part.xml", b"hello")])
    content = bytearray(path.read_bytes())
    central_directory = content.index(b"PK\x01\x02")
    # ZIP central-directory compressed size is at byte 20 of its entry header.
    # Change only this claim: expanded size stays five, compressed size becomes zero.
    struct.pack_into("<I", content, central_directory + 20, 0)
    path.write_bytes(content)
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        assert len(entries) == 1
        assert entries[0].file_size == 5
        assert entries[0].compress_size == 0
    before = path.read_bytes()
    for method in ("open", "read", "extract", "extractall"):
        monkeypatch.setattr(zipfile.ZipFile, method, forbidden_operation)
    outcome = ArchiveResourceProbe(ArchiveLimits(max_uncompressed_bytes=10, max_expansion_ratio=2)).check(
        path, detected_format=format_name
    )
    assert path.read_bytes() == before
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


@pytest.mark.parametrize("entries", [[], [("empty.xml", b"")], [("folder/", b"")],
                                    [("one.xml", b""), ("two.xml", b"")]])
@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_empty_archives_and_zero_length_members_remain_safe(archive_factory, entries, compression):
    path = archive_factory(entries=entries, compression=compression)
    outcome = ArchiveResourceProbe().check(path)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"archive_entries": len(entries), "uncompressed_bytes": 0, "expansion_ratio": 0.0}


@pytest.mark.parametrize("sizes", [[(5, 0)], [(2, 0), (3, 0)]])
@pytest.mark.parametrize("byte_limit", [4, 10])
def test_invalid_zero_total_is_unreadable_regardless_of_other_limits(monkeypatch, missing_path, sizes, byte_limit):
    document = use_archive(monkeypatch, sizes)
    outcome = ArchiveResourceProbe(ArchiveLimits(max_uncompressed_bytes=byte_limit)).check(missing_path)
    assert document.closed
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}
