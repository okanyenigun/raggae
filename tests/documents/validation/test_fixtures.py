def test_file_factory_creates_small_unicode_and_empty_inputs(file_factory):
    ordinary = file_factory("folder/résumé.txt", b"known bytes")
    empty = file_factory("empty.txt", b"")
    assert ordinary.read_bytes() == b"known bytes"
    assert ordinary.name == "résumé.txt"
    assert empty.read_bytes() == b""


def test_missing_path_fixture_does_not_create_a_file(missing_path):
    assert not missing_path.exists()


def test_finding_factory_creates_independent_results(finding_factory):
    first, second = finding_factory("test.first", count=1), finding_factory("test.second", count=2)
    assert first.code == "test.first"
    assert second.code == "test.second"
    assert first.detail["count"] == 1
    assert second.detail["count"] == 2
