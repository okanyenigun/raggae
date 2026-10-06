import pytest


@pytest.mark.parametrize("string_path", [False, True], ids=["path", "string"])
def test_explicit_empty_filename_reaches_worker_instead_of_disk_basename(
    inspector_factory, missing_path, string_path,
):
    assembly = inspector_factory()
    target = str(missing_path) if string_path else missing_path
    assembly.inspector.validate(target, submitted_filename="")
    assert assembly.calls[0] == ("test_filename", (), {"filename": ""})
