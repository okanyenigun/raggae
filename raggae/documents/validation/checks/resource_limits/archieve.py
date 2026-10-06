import zipfile
from pathlib import Path
from .policy import ArchiveLimits, ResourceFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome, FactValue, Finding


class ArchiveResourceProbe:
    """
    Entry count and declared sizes, read from the ZIP central directory. Nothing
    is extracted.

    The sizes are what the archive claims about itself. A lying archive is still
    caught, because extraction is bounded downstream — but the cheap refusal
    happens here, on the claim.
    """

    def __init__(self, policy: ArchiveLimits | None = None) -> None:
        self._policy = policy or ArchiveLimits()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)
        skipped = applicability(
            self.handles, detected_format, self.name, ResourceFinding.NOT_APPLICABLE
        )
        if skipped:
            return CheckOutcome(findings=tuple(skipped))

        try:
            with zipfile.ZipFile(target) as archive:
                entries = archive.infolist()
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(ResourceFinding.UNREADABLE, target, error),)
            )

        uncompressed = sum(entry.file_size for entry in entries)
        compressed = sum(entry.compress_size for entry in entries)
        if uncompressed > 0 and compressed == 0:
            return CheckOutcome(
                findings=(
                    unreadable(
                        ResourceFinding.UNREADABLE,
                        target,
                        ValueError("Archive declares expanded bytes without compressed bytes"),
                    ),
                )
            )
        ratio = uncompressed / compressed if compressed else 0.0

        facts: dict[str, FactValue] = {
            "archive_entries": len(entries),
            "uncompressed_bytes": uncompressed,
            "expansion_ratio": round(ratio, 2),
        }

        findings: list[Finding] = []
        findings += self._check_entries(len(entries))
        findings += self._check_uncompressed(uncompressed)
        findings += self._check_ratio(ratio)

        return CheckOutcome(findings=tuple(findings), facts=facts)

    @property
    def name(self) -> str:
        return "validation_resource_limits_archive"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"docx", "xlsx", "pptx"})

    @property
    def policy(self) -> ArchiveLimits:
        return self._policy

    def _check_entries(self, entries: int) -> list[Finding]:
        limit = self._policy.max_entries
        if entries <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.TOO_MANY_ENTRIES,
                "Archive holds more entries than the limit allows.",
                limit=limit,
                observed=entries,
            )
        ]

    def _check_uncompressed(self, uncompressed: int) -> list[Finding]:
        limit = self._policy.max_uncompressed_bytes
        if uncompressed <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.EXPANDS_TOO_LARGE,
                "Archive declares more expanded bytes than the limit allows.",
                limit=limit,
                observed=uncompressed,
            )
        ]

    def _check_ratio(self, ratio: float) -> list[Finding]:
        """
        Catch the bomb that stays under the byte limit by being tiny to begin
        with — a few kilobytes claiming a few hundred megabytes.
        """
        limit = self._policy.max_expansion_ratio
        if ratio <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.EXPANSION_RATIO_TOO_HIGH,
                "Archive expands by more than the limit allows.",
                limit=limit,
                observed=round(ratio, 2),
            )
        ]
