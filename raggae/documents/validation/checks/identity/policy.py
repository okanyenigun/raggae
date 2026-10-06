from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from enum import StrEnum
from ...schemas.result import Severity


class IdentityFinding(StrEnum):
    """What accessibility and identity can report."""

    NOT_FOUND = "identity.not_found"
    NOT_A_REGULAR_FILE = "identity.not_a_regular_file"
    SYMLINK = "identity.symlink"
    UNREADABLE = "identity.unreadable"
    TOO_LARGE = "identity.too_large"
    TOO_SMALL = "identity.too_small"
    CHANGED_WHILE_READING = "identity.changed_while_reading"


class IdentityPolicy(BaseModel):
    """Limits governing what may be opened and how it is fingerprinted."""

    model_config = ConfigDict(frozen=True)

    max_bytes: int = Field(
        default=100_000_000,
        gt=0,
        description="Largest file that may be opened.",
    )

    min_bytes: int = Field(
        default=1,
        ge=0,
        description="Smallest file that counts as a document.",
    )

    allow_symlinks: bool = Field(
        default=False,
        description="Whether a symlinked path is followed.",
    )

    compute_digest: bool = Field(
        default=True,
        description="Whether contents are hashed.",
    )

    hash_algorithm: str = Field(
        default="sha256",
        description="Which digest identifies the file.",
    )

    read_chunk_bytes: int = Field(
        default=1_048_576,
        gt=0,
        description="Bytes read per pass while hashing.",
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            IdentityFinding.NOT_FOUND: Severity.REJECT,
            IdentityFinding.NOT_A_REGULAR_FILE: Severity.REJECT,
            IdentityFinding.SYMLINK: Severity.REJECT,
            IdentityFinding.UNREADABLE: Severity.REJECT,
            IdentityFinding.TOO_LARGE: Severity.REJECT,
            IdentityFinding.TOO_SMALL: Severity.REJECT,
            IdentityFinding.CHANGED_WHILE_READING: Severity.REJECT,
        },
        description="Default decision severity for each file-identity finding.",
    )

    @field_validator("hash_algorithm", mode="after")
    @classmethod
    def _known_algorithm(cls, value: str) -> str:
        allowed_hash_algorithms = frozenset(
            {"sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b", "blake2s"}
        )

        normalized = value.strip().casefold()
        if normalized not in allowed_hash_algorithms:
            raise ValueError(
                f"Unsupported hash algorithm {value!r}. Choose one of: "
                + ", ".join(sorted(allowed_hash_algorithms))
            )
        return normalized

    @model_validator(mode="after")
    def _limits_are_orderable(self) -> "IdentityPolicy":
        if self.min_bytes > self.max_bytes:
            raise ValueError(
                f"min_bytes ({self.min_bytes}) cannot exceed "
                f"max_bytes ({self.max_bytes}); nothing would ever be accepted"
            )
        return self
