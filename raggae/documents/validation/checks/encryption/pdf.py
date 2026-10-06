import pikepdf
from pathlib import Path
from .policy import EncryptionPolicy, EncryptionFinding
from ..utils import require_path, normalize_extension
from ...schemas.result import CheckOutcome, Finding, FactValue


class PdfEncryptionProbe:
    """
    Opens the document and reports its encryption state: whether a password was
    needed, whether the supplied one worked, and what the permission flags allow.

    Permission flags are reported, never enforced. Nothing stops a library from
    extracting text regardless, so honouring them is a legal choice belonging to
    the caller.

    Opens strictly. pikepdf rebuilds a damaged cross-reference table by default;
    that is turned off here, so a broken file is reported rather than silently
    repaired into something we never received.
    """

    def __init__(self, policy: EncryptionPolicy | None = None) -> None:
        self._policy = policy or EncryptionPolicy()
        self._last_rc4_revision = 3
        self._weak_key_bits = 40

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)

        not_applicable = self._check_applicable(detected_format)
        if not_applicable:
            return CheckOutcome(findings=tuple(not_applicable))

        # The empty password first: most encrypted PDFs open with one, and that is
        # the difference between "locked" and "merely encrypted".
        try:
            document = self._open(target, "")
        except pikepdf.PasswordError:
            return self._inspect_locked(target, password)
        except Exception as error:
            return CheckOutcome(findings=(self._open_failure(target, error),))

        with document:
            return self._inspect_open(document, password_required=False)

    @property
    def name(self) -> str:
        return "validation_encryption_pdf"

    @property
    def policy(self) -> EncryptionPolicy:
        return self._policy

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"pdf"})

    def set_last_rc4_revision(self, revision: int) -> None:
        """Revision numbers at or below this are considered weak."""
        self._last_rc4_revision = revision

    def set_weak_key_bits(self, bits: int) -> None:
        """Key lengths at or below this are considered weak."""
        self._weak_key_bits = bits

    def _check_applicable(self, detected_format: str | None) -> list[Finding]:
        if detected_format is None:
            return []  # no claim made; the caller vouches for it
        fmt = normalize_extension(detected_format)
        if fmt in self.handles:
            return []
        return [
            self._finding(
                EncryptionFinding.NOT_APPLICABLE,
                f"Encryption is a PDF concept; this file is {fmt!r}.",
                detected_format=fmt,
            )
        ]

    def _inspect_locked(self, path: Path, password: str | None) -> CheckOutcome:
        """The empty password failed, so the file genuinely needs one."""
        facts: dict[str, FactValue] = {"encrypted": True, "password_required": True}

        if password is None:
            return CheckOutcome(
                findings=(
                    self._finding(
                        EncryptionFinding.PASSWORD_REQUIRED,
                        "Document needs a password and none was supplied.",
                    ),
                ),
                facts=facts,
            )

        try:
            document = self._open(path, password)
        except pikepdf.PasswordError:
            return CheckOutcome(
                findings=(
                    self._finding(
                        EncryptionFinding.PASSWORD_INCORRECT,
                        "The supplied password was not accepted.",
                    ),
                ),
                facts=facts,
            )
        except Exception as error:
            return CheckOutcome(findings=(self._open_failure(path, error),))

        with document:
            return self._inspect_open(document, password_required=True)

    def _inspect_open(
        self, document: pikepdf.Pdf, *, password_required: bool
    ) -> CheckOutcome:
        encrypted = bool(document.is_encrypted)
        extraction_allowed = bool(document.allow.extract)

        facts: dict[str, FactValue] = {
            "encrypted": encrypted,
            "password_required": password_required,
            "extraction_allowed": extraction_allowed,
        }

        findings: list[Finding] = []
        findings += self._check_password_accepted(password_required)

        if encrypted:
            encryption = document.encryption
            facts["encryption_bits"] = int(encryption.bits)
            facts["encryption_revision"] = int(encryption.R)
            findings += self._check_weak_encryption(encryption)

        findings += self._check_extraction(extraction_allowed)
        return CheckOutcome(findings=tuple(findings), facts=facts)

    def _check_password_accepted(self, password_required: bool) -> list[Finding]:
        """Report a locked document when the pipeline does not handle them."""
        if not password_required or self._policy.accept_password_protected:
            return []
        return [
            self._finding(
                EncryptionFinding.PASSWORD_PROTECTED,
                "Document is password-protected, which this pipeline does not accept.",
            )
        ]

    def _check_weak_encryption(self, encryption: object) -> list[Finding]:
        revision = int(getattr(encryption, "R", 0))
        bits = int(getattr(encryption, "bits", 0))
        if revision > self._last_rc4_revision and bits > self._weak_key_bits:
            return []
        return [
            self._finding(
                EncryptionFinding.WEAK_ENCRYPTION,
                f"Document uses obsolete encryption (revision {revision}, {bits} bits).",
                revision=revision,
                bits=bits,
            )
        ]

    def _check_extraction(self, extraction_allowed: bool) -> list[Finding]:
        if extraction_allowed:
            return []
        return [
            self._finding(
                EncryptionFinding.EXTRACTION_NOT_PERMITTED,
                "Document's permissions ask that its content not be extracted.",
            )
        ]

    def _open_failure(self, path: Path, error: BaseException) -> Finding:
        message = str(error).casefold()
        if "unsupported" in message or "not supported" in message:
            return self._finding(
                EncryptionFinding.UNSUPPORTED,
                f"Document uses an encryption scheme we cannot open: {error}.",
                path=str(path),
            )
        return self._finding(
            EncryptionFinding.UNREADABLE,
            f"Document could not be opened: {error}.",
            path=str(path),
        )

    @staticmethod
    def _finding(code: EncryptionFinding, message: str, **detail: FactValue) -> Finding:
        return Finding(code=code, message=message, detail=detail)

    @staticmethod
    def _open(path: Path, password: str) -> pikepdf.Pdf:
        return pikepdf.Pdf.open(path, password=password, attempt_recovery=False)
