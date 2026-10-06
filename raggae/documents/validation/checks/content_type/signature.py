import os
from pathlib import Path
from .policy import ContentTypePolicy, ContentTypeFinding
from ..utils import require_path, normalize_extension
from ...schemas.result import CheckOutcome, Finding, FactValue
from ...shared import TEXT_FORMATS


class SignatureContentTypeDetector:
    """
    Reads the prefix, matches it against the known signatures, and reports the
    format it found along with any disagreement with the claimed extension.

    Reads only ``policy.prefix_bytes`` and never opens the file as its format:
    this runs before the malware scan, so it must not hand bytes to a parser.

    Assumes use case 2 has already established the path is a readable regular
    file, and does not re-check it. What it cannot identify, it reports as
    undetermined rather than diagnosing.
    """

    def __init__(self, policy: ContentTypePolicy | None = None) -> None:
        self._policy = policy or ContentTypePolicy()
        self._signatures: tuple[tuple[bytes, str], ...] = (
            (b"\x89PNG\r\n\x1a\n", "png"),
            (b"%PDF-", "pdf"),
            (b"\xff\xd8\xff", "jpeg"),
            (b"II*\x00", "tiff"),
            (b"MM\x00*", "tiff"),
            (b"PK\x03\x04", "zip"),
            (b"PK\x05\x06", "zip"),  # empty archive
            (b"PK\x07\x08", "zip"),  # spanned archive
            # Formats we can name but will never accept. Worth identifying rather than
            # leaving undetermined: "this is a Windows executable called report.pdf" is a
            # far more useful answer than "unrecognized".
            (b"MZ", "exe"),
            (b"\x7fELF", "elf"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),  # legacy .doc/.xls/.ppt
            (b"\x1f\x8b", "gzip"),
            (b"Rar!\x1a\x07", "rar"),
            (b"7z\xbc\xaf\x27\x1c", "7z"),
        )
        self._ooxml_markers: tuple[tuple[bytes, str], ...] = (
            (b"word/", "docx"),
            (b"xl/", "xlsx"),
            (b"ppt/", "pptx"),
        )
        self._text_boms: tuple[bytes, ...] = (
            b"\xef\xbb\xbf",  # UTF-8
            b"\xff\xfe",  # UTF-16 LE
            b"\xfe\xff",  # UTF-16 BE
        )
        self._binary_control_bytes = frozenset(range(0x00, 0x20)) - {
            0x09,
            0x0A,
            0x0B,
            0x0C,
            0x0D,
        }

    # --- public interface ------------------------------------------------------

    def check(self, path: Path, claimed_extension: str | None = None) -> CheckOutcome:
        target = require_path(path)

        prefix = self._read_prefix(target)

        if prefix is None:
            return CheckOutcome(
                findings=(
                    self._finding(
                        ContentTypeFinding.UNREADABLE,
                        "File could not be read for content-type detection.",
                        path=str(target),
                    ),
                )
            )

        claimed = normalize_extension(claimed_extension) if claimed_extension else None

        detected, assumed = self._identify(prefix, claimed)

        findings: list[Finding] = []
        findings += self._check_identified(detected)
        findings += self._check_subtype_assumed(detected, assumed)
        findings += self._check_allowed(detected)
        findings += self._check_matches_claim(detected, claimed, claimed_extension)

        facts: dict[str, FactValue] = {}
        if detected is not None:
            facts["detected_format"] = detected

        return CheckOutcome(findings=tuple(findings), facts=facts)

    # --- internal helpers ------------------------------------------------------

    @property
    def name(self) -> str:
        return "validator_content_type_signature"

    @property
    def policy(self) -> ContentTypePolicy:
        return self._policy

    def set_signatures(self, signatures: tuple[tuple[bytes, str], ...]) -> None:
        """Override the default signature list for testing."""
        self._signatures = signatures

    def set_ooxml_markers(self, markers: tuple[tuple[bytes, str], ...]) -> None:
        """Override the default OOXML marker list for testing."""
        self._ooxml_markers = markers

    def set_text_boms(self, boms: tuple[bytes, ...]) -> None:
        """Override the default BOM list for testing."""
        self._text_boms = boms

    def set_binary_control_bytes(self, control_bytes: frozenset[int]) -> None:
        """Override the default binary control byte list for testing."""
        self._binary_control_bytes = control_bytes

    def _read_prefix(self, path: Path) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            with os.fdopen(os.open(path, flags), "rb") as stream:
                return stream.read(self._policy.prefix_bytes)
        except OSError:
            return None

    def _identify(self, prefix: bytes, claimed: str | None) -> tuple[str | None, bool]:
        """
        The format the bytes indicate, and whether a subtype had to be assumed.

        Signatures first, since they are conclusive. Text is the fallback because
        it has no signature at all — the absence of one is the whole signal.
        """
        signature = self._match_signature(prefix)
        if signature == "zip":
            subtype = self._match_ooxml_subtype(prefix)
            return (subtype, True) if subtype else ("zip", False)
        if signature is not None:
            return signature, False
        if self._looks_like_text(prefix):
            return self._identify_text(prefix, claimed), False
        return None, False

    def _identify_text(self, prefix: bytes, claimed: str | None) -> str:
        if self._policy.trust_extension_for_text and claimed in TEXT_FORMATS:
            return claimed  # type: ignore[return-value]

        head = prefix.lstrip()[:16].lower()
        if head.startswith(b"<"):
            return "html"
        if head.startswith((b"{", b"[")):
            return "json"
        return "txt"

    def _match_signature(self, prefix: bytes) -> str | None:
        """The format whose signature *prefix* starts with, if any."""
        for signature, fmt in self._signatures:
            if prefix.startswith(signature):
                return fmt
        return None

    def _match_ooxml_subtype(self, prefix: bytes) -> str | None:
        """The OOXML format whose part-tree marker appears in *prefix*, if any."""
        for marker, fmt in self._ooxml_markers:
            if marker in prefix:
                return fmt
        return None

    @staticmethod
    def _finding(
        code: ContentTypeFinding, message: str, **detail: FactValue
    ) -> Finding:
        return Finding(code=code, message=message, detail=detail)

    def _looks_like_text(self, prefix: bytes) -> bool:
        if not prefix:
            return False
        if prefix.startswith(self._text_boms):
            return True
        if self._binary_control_bytes.intersection(prefix):
            return False
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError as error:
            # Only an incomplete valid sequence can be caused by the read limit.
            # Invalid bytes remain invalid even when they occur at the end.
            return error.reason == "unexpected end of data" and error.end == len(prefix)
        return True

    def _check_identified(self, detected: str | None) -> list[Finding]:
        """Report a file whose bytes match nothing we recognize."""
        if detected is not None or not self._policy.require_known_format:
            return []
        return [
            self._finding(
                ContentTypeFinding.UNDETERMINED,
                "File matches no known signature and is not text.",
            )
        ]

    def _check_subtype_assumed(
        self, detected: str | None, assumed: bool
    ) -> list[Finding]:
        if not assumed or detected is None:
            return []
        return [
            self._finding(
                ContentTypeFinding.SUBTYPE_ASSUMED,
                f"Container is a ZIP; {detected!r} inferred from its part names.",
                format=detected,
            )
        ]

    def _check_allowed(self, detected: str | None) -> list[Finding]:
        """Report a format we can identify but do not accept."""
        if detected is None or detected in self._policy.allowed_formats:
            return []
        return [
            self._finding(
                ContentTypeFinding.NOT_ALLOWED,
                f"Detected format {detected!r} is not accepted.",
                format=detected,
            )
        ]

    def _check_matches_claim(
        self,
        detected: str | None,
        claimed: str | None,
        claimed_extension: str | None,
    ) -> list[Finding]:
        if detected is None or claimed_extension is None:
            return []
        if claimed == detected:
            return []
        return [
            self._finding(
                ContentTypeFinding.EXTENSION_MISMATCH,
                f"Filename claims {claimed_extension!r} but the bytes are "
                f"{detected!r}.",
                claimed=claimed_extension,
                detected=detected,
            )
        ]
