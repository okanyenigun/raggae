from pydantic import BaseModel, ConfigDict, Field
from enum import StrEnum
from ...schemas.result import Severity


class EncryptionFinding(StrEnum):
    """What encryption and access policy can report."""

    NOT_APPLICABLE = "encryption.not_applicable"
    UNREADABLE = "encryption.unreadable"
    UNSUPPORTED = "encryption.unsupported"
    PASSWORD_REQUIRED = "encryption.password_required"
    PASSWORD_INCORRECT = "encryption.password_incorrect"
    PASSWORD_PROTECTED = "encryption.password_protected"
    WEAK_ENCRYPTION = "encryption.weak_encryption"
    EXTRACTION_NOT_PERMITTED = "encryption.extraction_not_permitted"


class EncryptionPolicy(BaseModel):
    """Which encrypted documents are in scope."""

    model_config = ConfigDict(frozen=True)

    accept_password_protected: bool = Field(
        default=True,
        description=(
            "Whether documents needing a password are in scope at all. Says which "
            "inputs the pipeline handles, not what a locked document is worth — "
            "that judgment belongs to the coordinator."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            EncryptionFinding.UNREADABLE: Severity.REJECT,
            EncryptionFinding.UNSUPPORTED: Severity.REJECT,
            EncryptionFinding.PASSWORD_REQUIRED: Severity.REJECT,
            EncryptionFinding.PASSWORD_INCORRECT: Severity.REJECT,
            EncryptionFinding.PASSWORD_PROTECTED: Severity.REJECT,
            # About the document's own security, not about our ability to read it.
            EncryptionFinding.WEAK_ENCRYPTION: Severity.WARNING,
            # Advisory in the file and unenforceable in practice. Pipelines with a
            # legal obligation to honour it should raise this to reject.
            EncryptionFinding.EXTRACTION_NOT_PERMITTED: Severity.WARNING,
            EncryptionFinding.NOT_APPLICABLE: Severity.INFO,
        },
        description="Default decision severity for each encryption finding.",
    )

    recoverable: frozenset[str] = frozenset(
        {EncryptionFinding.PASSWORD_REQUIRED, EncryptionFinding.PASSWORD_INCORRECT}
    )
