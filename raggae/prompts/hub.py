"""Serve the versioned prompts packaged with raggae."""

import re
from importlib import resources
from pydantic import BaseModel, ConfigDict, Field


class Prompt(BaseModel):
    """One released version of a prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="Dotted prompt name; its folder under raggae/prompts.",
        examples=["ocr.transcription"],
    )
    version: str = Field(
        min_length=1,
        description="Released version, v1, v2, ...; a released version never changes.",
        examples=["v1"],
    )
    text: str = Field(min_length=1, description="The prompt text, exactly as sent.")


class PromptHub:
    """Look up the prompts stored as ``<name folders>/<version>.txt`` in this package.

    ``ocr.transcription`` version ``v1`` is ``ocr/transcription/v1.txt``. A
    released version file is never edited; a change is a new version. Without
    a version, ``get`` returns the latest (highest) one. The file's final line
    break is not part of the prompt.
    """

    def __init__(self) -> None:
        self._root = resources.files("raggae.prompts")
        self._name_pattern = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
        self._version_pattern = re.compile(r"v[1-9][0-9]*")

    def versions(self, name: str) -> tuple[str, ...]:
        """Released versions of ``name``, oldest first."""
        folder = self._folder(name)
        found = [
            entry.name.removesuffix(".txt")
            for entry in folder.iterdir()
            if entry.is_file()
            and entry.name.endswith(".txt")
            and self._version_pattern.fullmatch(entry.name.removesuffix(".txt"))
        ]
        if not found:
            raise LookupError(f"No prompt named {name!r}.")
        return tuple(sorted(found, key=lambda version: int(version[1:])))

    def get(self, name: str, version: str | None = None) -> Prompt:
        """The prompt ``name`` at ``version``, or its latest version."""
        if version is None:
            version = self.versions(name)[-1]
        elif not isinstance(version, str):
            raise TypeError("version must be a string such as 'v1', or None")
        elif not self._version_pattern.fullmatch(version):
            raise ValueError("version must look like 'v1', 'v2', ...")
        entry = self._folder(name).joinpath(f"{version}.txt")
        if not entry.is_file():
            raise LookupError(f"Prompt {name!r} has no version {version!r}.")
        text = entry.read_text(encoding="utf-8").removesuffix("\n")
        return Prompt(name=name, version=version, text=text)

    def _folder(self, name: str):
        if not isinstance(name, str):
            raise TypeError("name must be a string such as 'ocr.transcription'")
        if not self._name_pattern.fullmatch(name):
            raise ValueError("name must look like 'ocr.transcription'")
        folder = self._root.joinpath(*name.split("."))
        if not folder.is_dir():
            raise LookupError(f"No prompt named {name!r}.")
        return folder
