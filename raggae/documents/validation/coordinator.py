from pathlib import Path
from .checks.filename.base import FilenameValidator
from .checks.filename.strict import StrictFilenameValidator
from .checks.identity.base import IdentityProbe
from .checks.identity.file import FileIdentityProbe
from .checks.content_type.base import ContentTypeDetector
from .checks.content_type.signature import SignatureContentTypeDetector
from .checks.encryption.base import EncryptionValidator
from .checks.encryption.pdf import PdfEncryptionProbe
from .checks.encryption.policy import EncryptionFinding
from .checks.resource_limits.base import ResourceValidator
from .checks.resource_limits.archieve import ArchiveResourceProbe
from .checks.resource_limits.image import ImageResourceProbe
from .checks.resource_limits.pdf import PdfResourceProbe
from .checks.resource_limits.text import TextResourceProbe
from .checks.active_content.base import ActiveContentProbe
from .checks.active_content.csv import CsvActiveContentProbe
from .checks.active_content.html import HtmlActiveContentProbe
from .checks.active_content.ooxml import OoxmlActiveContentProbe
from .checks.active_content.pdf import PdfActiveContentProbe
from .checks.capability.base import CapabilityProbe
from .checks.capability.pdf import PdfCapabilityProbe
from .checks.utils import normalize_extension
from .schemas.result import (
    CheckRun,
    ValidationRun,
    CheckOutcome,
    WeightedFinding,
)
from .schemas.decision import DecisionPolicy
from .schemas.report import DocumentValidationReport


class DocumentInspector:
    def __init__(
        self,
        *,
        filename_validator: FilenameValidator | None = None,
        identity_probe: IdentityProbe | None = None,
        content_type_detector: ContentTypeDetector | None = None,
        encryption_probe: EncryptionValidator | None = None,
        resource_probes: tuple[ResourceValidator, ...] | None = None,
        active_content_probes: tuple[ActiveContentProbe, ...] | None = None,
        capability_probes: CapabilityProbe | None = None,
        policy: DecisionPolicy | None = None,
    ) -> None:
        self._filename_validator = (
            filename_validator
            if filename_validator is not None
            else StrictFilenameValidator()
        )
        self._identity_probe = (
            identity_probe if identity_probe is not None else FileIdentityProbe()
        )
        self._content_type_detector = (
            content_type_detector
            if content_type_detector is not None
            else SignatureContentTypeDetector()
        )
        self._encryption_probe = (
            encryption_probe
            if encryption_probe is not None
            else PdfEncryptionProbe()
        )
        self._resource_probes = (
            resource_probes
            if resource_probes is not None
            else (
                PdfResourceProbe(),
                ArchiveResourceProbe(),
                ImageResourceProbe(),
                TextResourceProbe(),
            )
        )
        self._active_content_probes = (
            active_content_probes
            if active_content_probes is not None
            else (
                PdfActiveContentProbe(),
                OoxmlActiveContentProbe(),
                HtmlActiveContentProbe(),
                CsvActiveContentProbe(),
            )
        )
        self._capability_probes = (
            capability_probes
            if capability_probes is not None
            else PdfCapabilityProbe()
        )
        self._policy = policy if policy is not None else DecisionPolicy()

    def validate(
        self,
        path: Path,
        submitted_filename: str | None = None,
        password: str | None = None,
    ) -> DocumentValidationReport:
        target = Path(path)
        run = ValidationRun()
        self._run_filename(
            run, target.name if submitted_filename is None else submitted_filename
        )
        self._run_identity(run, target)
        self._run_content_type(run, target)

        detected = run.facts.get("detected_format")
        detected_format = str(detected) if detected is not None else None

        self._run_encryption(run, target, detected_format, password)
        self._run_resource_limits(run, target, detected_format, password)
        self._run_active_content(run, target, detected_format, password)
        self._run_capability(run, target, detected_format, password)

        return DocumentValidationReport(
            decision=self._policy.decide(
                tuple(weighted.finding for weighted in run.findings)
            ),
            findings=tuple(run.findings),
            facts=run.facts,
            checks=tuple(run.checks),
        )

    def _run_filename(self, run: ValidationRun, submitted_filename: str) -> None:
        if self._halted(run):
            return
        if self._skip_disabled(run, self._filename_validator):
            return
        outcome = self._filename_validator.check(filename=submitted_filename)
        self._record(run, self._filename_validator.name, outcome)

    def _run_identity(self, run: ValidationRun, path: Path) -> None:
        if self._halted(run):
            return
        if self._skip_disabled(run, self._identity_probe):
            return
        outcome = self._identity_probe.check(path=path)
        self._record(run, self._identity_probe.name, outcome)

    def _run_content_type(self, run: ValidationRun, path: Path) -> None:
        if self._halted(run):
            return
        if self._skip_disabled(run, self._content_type_detector):
            return
        claimed = run.facts.get("claimed_extension")
        outcome = self._content_type_detector.check(
            path=path, claimed_extension=str(claimed) if claimed is not None else None
        )
        self._record(run, self._content_type_detector.name, outcome)

    def _run_encryption(
        self,
        run: ValidationRun,
        path: Path,
        detected_format: str | None,
        password: str | None,
    ) -> None:
        if self._halted(run):
            return
        wanted = self._format_or_skip(run, "encryption", detected_format)
        if wanted is None:
            return

        worker = self._encryption_probe
        if wanted not in worker.handles:
            self._record_uncovered(run, "encryption", wanted)
            return
        outcome = worker.check(path, wanted, password)
        self._record(run, worker.name, outcome)
        if wanted == "pdf" and (
            outcome.found(EncryptionFinding.PASSWORD_REQUIRED)
            or outcome.found(EncryptionFinding.PASSWORD_INCORRECT)
        ):
            run.pdf_access_blocked = True

    def _run_resource_limits(
        self,
        run: ValidationRun,
        path: Path,
        detected_format: str | None,
        password: str | None,
    ) -> None:
        if self._halted(run):
            return
        if not self._resource_probes:
            self._record_unconfigured(run, "resource limits")
            return
        wanted = self._format_or_skip(run, "resource limits", detected_format)
        if wanted is None:
            return

        for worker in self._resource_probes:
            if wanted in worker.handles:
                if self._skip_pdf_access(run, worker.name, wanted):
                    return
                self._record(run, worker.name, worker.check(path, wanted, password))
                return
        self._record_uncovered(run, "resource limits", wanted)

    def _run_active_content(
        self,
        run: ValidationRun,
        path: Path,
        detected_format: str | None,
        password: str | None,
    ) -> None:
        if self._halted(run):
            return
        if not self._active_content_probes:
            self._record_unconfigured(run, "active content")
            return
        wanted = self._format_or_skip(run, "active content", detected_format)
        if wanted is None:
            return

        for worker in self._active_content_probes:
            if wanted in worker.handles:
                if self._skip_pdf_access(run, worker.name, wanted):
                    return
                self._record(run, worker.name, worker.check(path, wanted, password))
                return
        self._record_uncovered(run, "active content", wanted)

    def _run_capability(
        self,
        run: ValidationRun,
        path: Path,
        detected_format: str | None,
        password: str | None,
    ) -> None:
        if self._halted(run):
            return
        wanted = self._format_or_skip(run, "capability", detected_format)
        if wanted is None:
            return

        worker = self._capability_probes
        if wanted not in worker.handles:
            self._record_uncovered(run, "capability", wanted)
            return
        if self._skip_pdf_access(run, worker.name, wanted):
            return
        self._record(run, worker.name, worker.check(path, wanted, password))

    @staticmethod
    def _skip_pdf_access(run: ValidationRun, check: str, wanted: str) -> bool:
        """Collection cannot inspect PDF internals without password access."""
        if wanted != "pdf" or not run.pdf_access_blocked:
            return False
        run.checks.append(
            CheckRun(
                check=check,
                ran=False,
                skipped_because="PDF access blocked: valid password required",
            )
        )
        return True

    @staticmethod
    def _skip_disabled(
        run: ValidationRun,
        worker: FilenameValidator | IdentityProbe | ContentTypeDetector,
    ) -> bool:
        """Honor an explicit opt-out without requiring it on custom workers."""
        if getattr(worker, "disabled", False) is not True:
            return False
        run.checks.append(
            CheckRun(check=worker.name, ran=False, skipped_because="check disabled")
        )
        return True

    @staticmethod
    def _format_or_skip(
        run: ValidationRun, step: str, detected_format: str | None
    ) -> str | None:
        if detected_format is None:
            run.checks.append(
                CheckRun(
                    check=step,
                    ran=False,
                    skipped_because="format not determined",
                )
            )
            return None
        return normalize_extension(detected_format)

    @staticmethod
    def _record_unconfigured(run: ValidationRun, step: str) -> None:
        run.checks.append(
            CheckRun(check=step, ran=False, skipped_because="no probe configured")
        )

    @staticmethod
    def _record_uncovered(run: ValidationRun, step: str, wanted: str) -> None:
        run.checks.append(
            CheckRun(
                check=step,
                ran=False,
                skipped_because=f"no probe covers {wanted!r}",
            )
        )

    def _record(self, run: ValidationRun, check: str, outcome: CheckOutcome) -> None:
        """Weigh what a check found, keep its facts, and note that it ran."""
        for finding in outcome.findings:
            run.findings.append(
                WeightedFinding(
                    finding=finding, severity=self._policy.severity_of(finding)
                )
            )
        self._merge_facts(run, outcome)
        run.checks.append(CheckRun(check=check, ran=True))

        if self._policy.stop_on_first_rejection and any(
            self._policy.rejects(finding) for finding in outcome.findings
        ):
            run.stopped = True

    @staticmethod
    def _halted(run: ValidationRun) -> bool:
        return run.stopped

    @staticmethod
    def _merge_facts(run: ValidationRun, outcome: CheckOutcome) -> None:
        for key, value in outcome.facts.items():
            existing = run.facts.get(key)
            if key in run.facts and (
                type(existing) is not type(value) or existing != value
            ):
                raise RuntimeError(
                    f"Checks disagree about {key!r}: {existing!r} then {value!r}"
                )
            run.facts[key] = value
