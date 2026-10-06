import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from urllib.error import HTTPError
import zipfile

import pytest

from scripts.ci import check_release as checks


SCRIPT = Path(checks.__file__).resolve()


def run_git(repository, *args):
    return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()


@pytest.fixture
def release_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    run_git(repository, "init", "-b", "master")
    run_git(repository, "config", "user.name", "Release test")
    run_git(repository, "config", "user.email", "release@example.invalid")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "raggae"\nversion = "0.1.1"\n', encoding="utf-8",
    )
    run_git(repository, "add", "pyproject.toml")
    run_git(repository, "commit", "-m", "Version 0.1.1")
    commit = run_git(repository, "rev-parse", "HEAD")
    run_git(repository, "update-ref", "refs/remotes/origin/master", commit)
    return repository, commit


@pytest.mark.parametrize("annotated", [False, True])
def test_tagged_master_commit_is_accepted(release_repository, annotated):
    repository, commit = release_repository
    args = ("tag", "-a", "v0.1.1", "-m", "Release") if annotated else ("tag", "v0.1.1")
    run_git(repository, *args)
    assert checks.verify_release("v0.1.1", repository) == (commit, "0.1.1")


def test_older_merged_master_commit_is_accepted(release_repository):
    repository, commit = release_repository
    run_git(repository, "tag", "v0.1.1")
    run_git(repository, "commit", "--allow-empty", "-m", "Later master change")
    run_git(repository, "update-ref", "refs/remotes/origin/master", "HEAD")
    assert checks.verify_release("v0.1.1", repository) == (commit, "0.1.1")


def test_unmerged_feature_tag_is_rejected(release_repository):
    repository, _ = release_repository
    run_git(repository, "switch", "-c", "feature")
    run_git(repository, "commit", "--allow-empty", "-m", "Unmerged change")
    run_git(repository, "tag", "v0.1.1")
    with pytest.raises(ValueError, match="not on origin/master"):
        checks.verify_release("v0.1.1", repository)


@pytest.mark.parametrize("tag", [
    "0.1.1", "v01.1.1", "v0.01.1", "v0.1.01", "v0.1", "v0.1.1rc1",
    "v0.1.1.dev1", "v0.1.1\n", "v٠.١.١", "master", "--help", "v0.1.1;echo BAD",
])
def test_invalid_tags_fail_before_git_access(tag, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid tag reached Git")

    monkeypatch.setattr(checks, "git", forbidden)
    with pytest.raises(ValueError, match="stable versions"):
        checks.verify_release(tag, tmp_path)


@pytest.mark.parametrize("project_name,tag", [("other", "v0.1.1"), ("raggae", "v0.1.2")])
def test_name_or_version_mismatch_is_rejected(release_repository, project_name, tag):
    repository, _ = release_repository
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "0.1.1"\n', encoding="utf-8",
    )
    run_git(repository, "add", "pyproject.toml")
    run_git(repository, "commit", "--allow-empty", "-m", "Metadata")
    run_git(repository, "update-ref", "refs/remotes/origin/master", "HEAD")
    run_git(repository, "tag", tag)
    with pytest.raises(ValueError, match="must match"):
        checks.verify_release(tag, repository)


def test_missing_master_reference_fails_closed(release_repository):
    repository, _ = release_repository
    run_git(repository, "tag", "v0.1.1")
    run_git(repository, "update-ref", "-d", "refs/remotes/origin/master")
    with pytest.raises(ValueError, match="master cannot be verified"):
        checks.verify_release("v0.1.1", repository)


def test_cli_outputs_verified_commit_and_version(release_repository, tmp_path):
    repository, commit = release_repository
    run_git(repository, "tag", "v0.1.1")
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "release", "--tag", "v0.1.1", "--github-output", str(output)],
        cwd=repository, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"commit={commit}\nversion=0.1.1\n"


def test_failed_cli_does_not_write_release_outputs(release_repository, tmp_path):
    repository, _ = release_repository
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "release", "--tag", "feature", "--github-output", str(output)],
        cwd=repository, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert not output.exists()


@pytest.mark.parametrize("name", [
    ".env", "raggae/.env.local", ".git/config", ".venv/config", "temp/blog.txt",
    "../secret", "/absolute/secret", "raggae/../../secret", "raggae/__pycache__/module.pyc",
])
def test_private_or_unsafe_archive_paths_are_rejected(name):
    with pytest.raises(ValueError, match="Private, unsafe"):
        checks.check_file(name, b"ordinary content")


def test_credential_content_is_rejected_without_echoing_it():
    fake_token = b"pypi-" + b"x" * 40
    with pytest.raises(ValueError) as error:
        checks.check_file("README.md", fake_token)
    assert fake_token.decode() not in str(error.value)


def test_ordinary_package_file_is_allowed():
    checks.check_file("raggae/__init__.py", b"# ordinary source")


@pytest.fixture
def distributions(tmp_path):
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "raggae").mkdir()
    content = b"# package source\n"
    (repository / "raggae/__init__.py").write_bytes(content)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "raggae"\nversion = "0.1.1"\nrequires-python = ">=3.13"\n'
        'dependencies = ["pydantic==2.13.4"]\n', encoding="utf-8",
    )
    directory = repository / "dist"
    directory.mkdir()
    metadata = b"Name: raggae\nVersion: 0.1.1\nRequires-Python: >=3.13\nRequires-Dist: pydantic==2.13.4\n\n"

    def build(*, wheel_extra=None, source_extra=None, wheel_metadata=metadata, wheel_content=content):
        with zipfile.ZipFile(directory / "raggae-0.1.1-py3-none-any.whl", "w") as archive:
            archive.writestr("raggae/__init__.py", wheel_content)
            archive.writestr("raggae-0.1.1.dist-info/METADATA", wheel_metadata)
            for name, value in (wheel_extra or {}).items():
                archive.writestr(name, value)
        with tarfile.open(directory / "raggae-0.1.1.tar.gz", "w:gz") as archive:
            files = {"raggae-0.1.1/PKG-INFO": metadata, "raggae-0.1.1/raggae/__init__.py": content}
            files.update(source_extra or {})
            for name, value in files.items():
                member = tarfile.TarInfo(name)
                if value is None:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/private/secret"
                    archive.addfile(member)
                else:
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
        return directory, repository

    return build


def test_matching_distributions_are_accepted(distributions):
    directory, repository = distributions()
    assert checks.verify_distributions(directory, repository) == "0.1.1"


def test_uv_gitignore_marker_is_allowed(distributions):
    directory, repository = distributions()
    (directory / ".gitignore").write_text("*", encoding="utf-8")
    assert checks.verify_distributions(directory, repository) == "0.1.1"


@pytest.mark.parametrize("extra", [".env", ".gitignore.old", "unexpected.txt"])
def test_uv_marker_does_not_allow_other_extra_files(distributions, extra):
    directory, repository = distributions()
    (directory / ".gitignore").write_text("*", encoding="utf-8")
    (directory / extra).write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        checks.verify_distributions(directory, repository)


@pytest.mark.parametrize("location", ["wheel", "source"])
def test_env_in_either_distribution_is_rejected(distributions, location):
    kwargs = {f"{location}_extra": {".env" if location == "wheel" else "raggae-0.1.1/.env": b"private"}}
    directory, repository = distributions(**kwargs)
    with pytest.raises(ValueError, match="Private, unsafe"):
        checks.verify_distributions(directory, repository)


def test_source_archive_symlink_is_rejected(distributions):
    directory, repository = distributions(source_extra={"raggae-0.1.1/link": None})
    with pytest.raises(ValueError, match="link, special file"):
        checks.verify_distributions(directory, repository)


@pytest.mark.parametrize("change", [
    (b"Name: raggae", b"Name: other"),
    (b"Version: 0.1.1", b"Version: 0.1.2"),
    (b"Requires-Python: >=3.13", b"Requires-Python: >=3.12"),
    (b"Requires-Dist: pydantic==2.13.4", b""),
])
def test_incorrect_metadata_is_rejected(distributions, change):
    old, new = change
    metadata = b"Name: raggae\nVersion: 0.1.1\nRequires-Python: >=3.13\nRequires-Dist: pydantic==2.13.4\n\n"
    directory, repository = distributions(wheel_metadata=metadata.replace(old, new))
    with pytest.raises(ValueError, match="metadata"):
        checks.verify_distributions(directory, repository)


def test_changed_package_module_is_rejected(distributions):
    directory, repository = distributions(wheel_content=b"# changed source\n")
    with pytest.raises(ValueError, match="module is missing or changed"):
        checks.verify_distributions(directory, repository)


def test_extra_release_file_is_rejected(distributions):
    directory, repository = distributions()
    (directory / "unexpected.txt").write_text("extra")
    with pytest.raises(ValueError, match="exactly one"):
        checks.verify_distributions(directory, repository)


def pypi_data(directory):
    return {
        "info": {"name": "raggae", "version": "0.1.1"},
        "urls": [{"filename": path.name, "yanked": False,
                  "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
                 for path in directory.iterdir()],
    }


def test_matching_publication_hashes_are_accepted(distributions):
    directory, _ = distributions()
    checks.check_pypi_response(pypi_data(directory), directory, "0.1.1")


@pytest.mark.parametrize("problem", ["name", "version", "missing-file", "hash", "yanked", "unsafe-filename"])
def test_wrong_publication_is_rejected(distributions, problem):
    directory, _ = distributions()
    data = pypi_data(directory)
    if problem in {"name", "version"}:
        data["info"][problem] = "wrong"
    elif problem == "missing-file":
        data["urls"].pop()
    elif problem == "hash":
        data["urls"][0]["digests"]["sha256"] = "wrong"
    elif problem == "yanked":
        data["urls"][0]["yanked"] = True
    else:
        data["urls"][0]["filename"] = "../secret"
    with pytest.raises(ValueError):
        checks.check_pypi_response(data, directory, "0.1.1")


def test_publication_404_is_retried_without_network_or_sleep(distributions, monkeypatch):
    directory, _ = distributions()
    calls, sleeps = [], []

    def response(url, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise HTTPError(url, 404, "Not yet visible", {}, None)
        return io.BytesIO(json.dumps(pypi_data(directory)).encode())

    monkeypatch.setattr(checks, "urlopen", response)
    monkeypatch.setattr(checks.time, "sleep", sleeps.append)
    checks.verify_pypi(directory, "0.1.1")
    assert calls == [("https://pypi.org/pypi/raggae/0.1.1/json", 10)] * 2
    assert sleeps == [5]


def test_publication_retry_is_bounded(tmp_path, monkeypatch):
    calls, sleeps = [], []

    def unavailable(url, timeout):
        calls.append(url)
        raise HTTPError(url, 404, "Missing", {}, None)

    monkeypatch.setattr(checks, "urlopen", unavailable)
    monkeypatch.setattr(checks.time, "sleep", sleeps.append)
    with pytest.raises(ValueError, match="six attempts"):
        checks.verify_pypi(tmp_path, "0.1.1")
    assert len(calls) == 6
    assert sleeps == [5] * 5
