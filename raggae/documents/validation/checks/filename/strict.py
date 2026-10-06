import re
import unicodedata
from pathlib import PurePosixPath
from .policy import FilenamePolicy, FilenameFinding
from ...schemas.result import CheckOutcome, Finding, FactValue


class StrictFilenameValidator:
    """
    Applies the policy and reports everything it finds.

    Collects every problem rather than stopping at the first: a caller fixing a
    name would rather see all the reasons at once. The exception is an empty
    name, where nothing after it is meaningful.
    """

    def __init__(self, policy: FilenamePolicy | None = None) -> None:
        self._policy = policy or FilenamePolicy()
        self._encoded_separator = re.compile(
            r"%(?:00|0a|0d|2f|5c)", flags=re.IGNORECASE
        )
        self._hidden_codepoints = frozenset("­؜​‌‍‎‏‪‫‬‭‮⁠⁡⁢⁣⁤⁦⁧⁨⁩﻿")
        self._drive_prefix = re.compile(r"^[a-zA-Z]:")

    # --- public interface ------------------------------------------------------

    def check(self, filename: str) -> CheckOutcome:
        self._require_string(filename)

        if filename == "":
            return CheckOutcome(findings=(self._empty(),))

        findings: list[Finding] = []
        findings += self._check_input_length(filename)
        findings += self._check_encoded_separator(filename)
        findings += self._check_disguised_characters(filename)
        basename, had_components = self._split_basename(filename)

        findings += self._check_path_components(had_components)

        normalized = unicodedata.normalize("NFC", basename)
        findings += self._check_normalization(basename, normalized)

        canonical = normalized.strip()
        findings += self._check_whitespace(normalized, canonical)

        findings += self._check_basename(canonical)
        findings += self._check_name_length(canonical)
        try:
            findings += self._check_name_bytes(canonical)
        except UnicodeEncodeError:
            # Invalid Unicode has no UTF-8 byte length. Preserve the input and
            # report it, including when an earlier hidden character was found first.
            if not any(f.code == FilenameFinding.CONTROL_CHARACTER for f in findings):
                findings.append(
                    self._finding(
                        FilenameFinding.CONTROL_CHARACTER,
                        "Filename contains a character that cannot be encoded as UTF-8.",
                    )
                )

        suffixes = self.suffixes(canonical)

        claimed_extension = suffixes[-1] if suffixes else None
        findings += self._check_dangerous_extensions(suffixes)
        findings += self._check_claimed_extension(claimed_extension)

        return CheckOutcome(
            findings=tuple(findings),
            facts=self._facts(canonical, claimed_extension),
        )

    # --- internal helpers ------------------------------------------------------

    @property
    def name(self) -> str:
        return "validator_filename_strict"

    @property
    def policy(self) -> FilenamePolicy:
        return self._policy

    def set_encoded_separator_pattern(self, pattern: str) -> None:
        self._encoded_separator = re.compile(pattern, flags=re.IGNORECASE)
        return

    def set_hidden_codepoints(self, codepoints: str) -> None:
        self._hidden_codepoints = frozenset(codepoints)
        return

    def set_drive_prefix_pattern(self, pattern: str) -> None:
        self._drive_prefix = re.compile(pattern)
        return

    @staticmethod
    def _require_string(value: object) -> None:
        if not isinstance(value, str):
            raise TypeError(
                f"submitted_filename must be a string, got {type(value).__name__}"
            )

    @staticmethod
    def suffixes(filename: str) -> tuple[str, ...]:
        return tuple(
            suffix.removeprefix(".").casefold()
            for suffix in PurePosixPath(filename.rstrip(" .")).suffixes
            if suffix != "."
        )

    @staticmethod
    def _finding(code: FilenameFinding, message: str, **detail: FactValue) -> Finding:
        return Finding(code=code, message=message, detail=detail)

    @staticmethod
    def _facts(canonical: str, claimed: str | None) -> dict[str, FactValue]:
        facts: dict[str, FactValue] = {}
        if canonical:
            facts["canonical_filename"] = canonical
        if claimed is not None:
            facts["claimed_extension"] = claimed
        return facts

    def _empty(self) -> Finding:
        return self._finding(FilenameFinding.EMPTY, "No filename was supplied.")

    def _split_basename(self, filename: str) -> tuple[str, bool]:
        had_components = (
            "/" in filename
            or "\\" in filename
            or bool(self._drive_prefix.match(filename))
        )
        return re.split(r"[\\/]", self._drive_prefix.sub("", filename))[
            -1
        ], had_components

    # --- checks on the raw submitted name ------------------------------------

    def _check_input_length(self, submitted: str) -> list[Finding]:
        limit = self._policy.max_submitted_chars
        if len(submitted) <= limit:
            return []
        return [
            self._finding(
                FilenameFinding.INPUT_TOO_LONG,
                "Submitted filename exceeds the input-length limit.",
                limit=limit,
                observed=len(submitted),
            )
        ]

    def _check_encoded_separator(self, submitted: str) -> list[Finding]:
        match = self._encoded_separator.search(submitted)
        if match is None:
            return []
        return [
            self._finding(
                FilenameFinding.ENCODED_SEPARATOR,
                "Filename contains an encoded control character or path separator.",
                sequence=match.group(0),
            )
        ]

    def _check_disguised_characters(self, submitted: str) -> list[Finding]:
        for character in submitted:
            if unicodedata.category(character) in {"Cc", "Cs"}:
                return [
                    self._finding(
                        FilenameFinding.CONTROL_CHARACTER,
                        "Filename contains a control character.",
                    )
                ]
            if character in self._hidden_codepoints:
                return [
                    self._finding(
                        FilenameFinding.HIDDEN_CHARACTER,
                        "Filename contains a hidden or directional character.",
                    )
                ]
        return []

    def _check_path_components(self, had_components: bool) -> list[Finding]:
        if not had_components or not self._policy.reject_path_components:
            return []
        return [
            self._finding(
                FilenameFinding.PATH_COMPONENTS,
                "Filename carried directory components, which were stripped.",
            )
        ]

    def _check_normalization(self, before: str, after: str) -> list[Finding]:
        if before == after:
            return []
        return [
            self._finding(
                FilenameFinding.NORMALIZED, "Filename was normalized to NFC form."
            )
        ]

    def _check_whitespace(self, before: str, after: str) -> list[Finding]:
        if before == after:
            return []
        return [
            self._finding(
                FilenameFinding.WHITESPACE_TRIMMED,
                "Surrounding whitespace was removed.",
            )
        ]

    def _check_basename(self, canonical: str) -> list[Finding]:
        if canonical not in {"", ".", ".."}:
            return []
        return [
            self._finding(
                FilenameFinding.INVALID_BASENAME,
                "Filename does not contain a usable basename.",
            )
        ]

    def _check_name_length(self, canonical: str) -> list[Finding]:
        limit = self._policy.max_name_chars
        if len(canonical) <= limit:
            return []
        return [
            self._finding(
                FilenameFinding.NAME_TOO_LONG,
                "Canonical filename exceeds the character limit.",
                limit=limit,
                observed=len(canonical),
            )
        ]

    def _check_name_bytes(self, canonical: str) -> list[Finding]:
        limit = self._policy.max_name_utf8_bytes
        observed = len(canonical.encode("utf-8"))
        if observed <= limit:
            return []
        return [
            self._finding(
                FilenameFinding.NAME_TOO_MANY_BYTES,
                "Canonical filename exceeds the UTF-8 byte limit.",
                limit=limit,
                observed=observed,
            )
        ]

    def _check_dangerous_extensions(self, suffixes: tuple[str, ...]) -> list[Finding]:
        dangerous = sorted(set(suffixes) & self._policy.dangerous_extensions)
        if not dangerous:
            return []
        return [
            self._finding(
                FilenameFinding.DANGEROUS_EXTENSION,
                "Filename carries a prohibited extension: " + ", ".join(dangerous),
                extensions=", ".join(dangerous),
            )
        ]

    def _check_claimed_extension(self, claimed: str | None) -> list[Finding]:
        if claimed is None:
            if not self._policy.require_extension:
                return []
            return [
                self._finding(
                    FilenameFinding.EXTENSION_MISSING,
                    "Filename does not contain an extension.",
                )
            ]
        if claimed in self._policy.allowed_extensions:
            return []
        return [
            self._finding(
                FilenameFinding.EXTENSION_NOT_ALLOWED,
                f"Extension {claimed!r} is not accepted.",
                extension=claimed,
            )
        ]
