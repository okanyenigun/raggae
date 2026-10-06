"""Read-only release guards, archive checks, and isolated-install smoke tests."""

import argparse
import hashlib
import importlib
from importlib.metadata import version as installed_version
import json
from pathlib import Path, PurePosixPath
import pkgutil
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import tomllib
from email.parser import BytesParser
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import zipfile


STABLE_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
PRIVATE_PARTS = {".git", ".venv", ".aws", ".codex", ".agents", "temp", "__pycache__"}
TOKEN = re.compile(rb"pypi-[A-Za-z0-9_-]{20,}")


def git(*args: str, repository: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()


def verify_release(tag: str, repository: Path) -> tuple[str, str]:
    """Accept stable version tags only when their commit is already on master."""
    if not STABLE_TAG.fullmatch(tag):
        raise ValueError("Release tags must be stable versions such as v0.1.1.")
    commit = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", repository=repository)
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/master"],
        cwd=repository, capture_output=True,
    )
    if ancestry.returncode != 0:
        raise ValueError("The release commit is not on origin/master, or master cannot be verified.")
    project = tomllib.loads(git("show", f"{commit}:pyproject.toml", repository=repository))["project"]
    if project["name"] != "raggae" or project["version"] != tag[1:]:
        raise ValueError("The release tag must match raggae's version in pyproject.toml.")
    return commit, project["version"]


def check_file(name: str, content: bytes) -> None:
    parts = PurePosixPath(name).parts
    if (
        not parts or PurePosixPath(name).is_absolute() or ".." in parts
        or PRIVATE_PARTS.intersection(parts)
        or any(part == ".env" or part.startswith(".env.") for part in parts)
        or TOKEN.search(content)
    ):
        # Do not print file contents or a matched credential.
        raise ValueError("Private, unsafe, or credential-bearing content found in a release archive.")


def read_distributions(directory: Path, release_version: str) -> tuple[dict[str, bytes], dict[str, bytes]]:
    expected = {f"raggae-{release_version}-py3-none-any.whl", f"raggae-{release_version}.tar.gz"}
    # uv creates this directory-local marker; it is never a release artifact.
    files = {path.name for path in directory.iterdir() if path.is_file()}
    if files - {".gitignore"} != expected:
        raise ValueError("The release directory must contain exactly one raggae wheel and source archive.")
    with zipfile.ZipFile(directory / f"raggae-{release_version}-py3-none-any.whl") as archive:
        names = [member.filename for member in archive.infolist() if not member.is_dir()]
        if len(set(names)) != len(names) or archive.testzip() is not None:
            raise ValueError("The wheel contains duplicate entries or corrupt data.")
        wheel = {name: archive.read(name) for name in names}
    with tarfile.open(directory / f"raggae-{release_version}.tar.gz", "r:gz") as archive:
        source = {}
        for member in archive.getmembers():
            check_file(member.name, b"")
            if member.isdir():
                continue
            if not member.isfile() or member.name in source:
                raise ValueError("The source archive contains a link, special file, or duplicate entry.")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Cannot read a source archive member.")
            source[member.name] = stream.read()
    for files in (wheel, source):
        for name, content in files.items():
            check_file(name, content)
    return wheel, source


def verify_distributions(directory: Path, repository: Path) -> str:
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    release_version = project["version"]
    if project["name"] != "raggae" or not STABLE_TAG.fullmatch(f"v{release_version}"):
        raise ValueError("Expected raggae with a stable release version.")
    wheel, source = read_distributions(directory, release_version)
    for files, metadata_name in (
        (wheel, f"raggae-{release_version}.dist-info/METADATA"),
        (source, f"raggae-{release_version}/PKG-INFO"),
    ):
        metadata = BytesParser().parsebytes(files[metadata_name])
        if (metadata["Name"], metadata["Version"], metadata["Requires-Python"]) != (
            "raggae", release_version, project["requires-python"],
        ):
            raise ValueError("Distribution metadata does not match pyproject.toml.")
        declared = {value.replace(" ", "") for value in metadata.get_all("Requires-Dist", [])}
        if not {value.replace(" ", "") for value in project["dependencies"]}.issubset(declared):
            raise ValueError("Distribution metadata is missing a required dependency.")
    for path in (repository / "raggae").rglob("*.py"):
        name = path.relative_to(repository).as_posix()
        content = path.read_bytes()
        if wheel.get(name) != content or source.get(f"raggae-{release_version}/{name}") != content:
            raise ValueError("A package module is missing or changed in a distribution.")
    return release_version


def check_pypi_response(data: dict, directory: Path, release_version: str) -> None:
    if (data["info"]["name"], data["info"]["version"]) != ("raggae", release_version):
        raise ValueError("PyPI returned a different package or version.")
    expected = {f"raggae-{release_version}-py3-none-any.whl", f"raggae-{release_version}.tar.gz"}
    if {item["filename"] for item in data["urls"]} != expected:
        raise ValueError("The published release does not contain both expected distributions.")
    for item in data["urls"]:
        if item["yanked"] or item["digests"]["sha256"] != hashlib.sha256(
            (directory / item["filename"]).read_bytes()
        ).hexdigest():
            raise ValueError("A published distribution is yanked or differs from the verified build.")


def verify_pypi(directory: Path, release_version: str) -> None:
    if not STABLE_TAG.fullmatch(f"v{release_version}"):
        raise ValueError("Expected a stable release version.")
    for attempt in range(6):
        try:
            with urlopen(f"https://pypi.org/pypi/raggae/{release_version}/json", timeout=10) as response:
                data = json.load(response)
        except HTTPError as error:
            if error.code != 404:
                raise
        except URLError:
            pass
        else:
            check_pypi_response(data, directory, release_version)
            return
        if attempt < 5:
            print("Waiting for the public PyPI release metadata...", flush=True)
            time.sleep(5)
    raise ValueError("The PyPI release could not be verified after six attempts.")


def smoke_test(release_version: str) -> None:
    # Call with the fresh environment's python -I, never an editable installation.
    import raggae
    from raggae.documents.validation import Decision, DocumentInspector

    if installed_version("raggae") != release_version:
        raise ValueError("The installed package has a different version.")
    if not Path(raggae.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("The smoke test imported a source checkout, not an installed distribution.")
    for module in pkgutil.walk_packages(raggae.__path__, raggae.__name__ + "."):
        importlib.import_module(module.name)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "document.txt"
        path.write_text("Ordinary document text.\n", encoding="utf-8")
        report = DocumentInspector().validate(path)
        if report.decision is not Decision.ACCEPT or report.facts["detected_format"] != "txt":
            raise ValueError("The installed inspector did not accept an ordinary text document.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release")
    release.add_argument("--tag", required=True)
    release.add_argument("--github-output", type=Path)
    dist = commands.add_parser("dist")
    dist.add_argument("--directory", type=Path, default=Path("dist"))
    pypi = commands.add_parser("pypi")
    pypi.add_argument("--directory", type=Path, default=Path("dist"))
    pypi.add_argument("--version", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--version")
    args = parser.parse_args()
    if args.command == "release":
        commit, release_version = verify_release(args.tag, Path.cwd())
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"commit={commit}\nversion={release_version}\n")
    elif args.command == "dist":
        release_version = verify_distributions(args.directory, Path.cwd())
    elif args.command == "pypi":
        release_version = args.version
        verify_pypi(args.directory, release_version)
    else:
        release_version = args.version or tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        smoke_test(release_version)
    print(f"{args.command} verification passed for raggae {release_version}.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release check failed: {error}", file=sys.stderr)
        sys.exit(1)
