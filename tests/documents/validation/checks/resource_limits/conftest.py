import zipfile

import pytest


@pytest.fixture
def archive_factory(tmp_path):
    """Small ZIP central-directory fixtures; no extraction is needed."""
    def make_archive(name="document.docx", *, entries=(), compression=zipfile.ZIP_STORED):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for member, content in entries:
                archive.writestr(member, content)
        return path

    return make_archive
