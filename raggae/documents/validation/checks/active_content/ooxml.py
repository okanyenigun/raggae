import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from .base import reference_finding
from .policy import ActiveContentPolicy, ActiveContentFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome, Finding


class _RelationshipTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, public_id: str | None, system_id: str | None) -> None:
        # Relationship parts do not need DTDs or user-defined entity expansion.
        raise ValueError("DTD declarations are not allowed in relationship XML")


class OoxmlActiveContentProbe:
    """
    Reads the archive's part names and its relationship files.

    Only the relationship XML is decompressed — a few hundred bytes naming what
    the document points at. The document body is left alone.
    """

    def __init__(self, policy: ActiveContentPolicy | None = None) -> None:
        self._policy = policy or ActiveContentPolicy()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)
        skipped = applicability(
            self.handles,
            detected_format,
            self.name,
            ActiveContentFinding.NOT_APPLICABLE,
        )
        if skipped:
            return CheckOutcome(findings=tuple(skipped))

        try:
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                relationships = self._read_relationships(archive, names)
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(ActiveContentFinding.UNREADABLE, target, error),)
            )

        findings: list[Finding] = []
        findings += self._check_macros(names)
        findings += self._check_templates(relationships)
        findings += reference_finding(
            [target for _, target in relationships], self._policy
        )
        return CheckOutcome(findings=tuple(findings))

    @property
    def name(self) -> str:
        return "validation_active_content_ooxml"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"docx", "xlsx", "pptx"})

    @property
    def policy(self) -> ActiveContentPolicy:
        return self._policy

    @staticmethod
    def _read_relationships(
        archive: zipfile.ZipFile, names: list[str]
    ) -> list[tuple[str, str]]:
        """External relationships, as (type, target) pairs."""
        found: list[tuple[str, str]] = []
        for name in names:
            if not name.endswith(".rels"):
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            parser = ET.XMLParser(target=_RelationshipTreeBuilder())
            root = ET.fromstring(content, parser=parser)
            if root.tag.rsplit("}", 1)[-1] != "Relationships":
                continue
            # Match children in the root's namespace (or unqualified XML).
            relationship_tag = root.tag.removesuffix("Relationships") + "Relationship"
            for relationship in root:
                if relationship.tag != relationship_tag:
                    continue
                if relationship.get("TargetMode") != "External":
                    continue
                kind = relationship.get("Type", "")
                target = relationship.get("Target", "")
                if target:
                    found.append((kind, target))
        return found

    @staticmethod
    def _check_macros(names: list[str]) -> list[Finding]:
        if not any(name.endswith("vbaProject.bin") for name in names):
            return []
        return [
            make_finding(
                ActiveContentFinding.MACRO,
                "Document carries a VBA macro project.",
            )
        ]

    @staticmethod
    def _check_templates(relationships: list[tuple[str, str]]) -> list[Finding]:
        """
        A template loaded from a URL is fetched when the document opens, which is
        both a beacon and a way to deliver content the document does not contain.
        """
        templates = [
            target
            for kind, target in relationships
            if kind.endswith("attachedTemplate")
        ]
        if not templates:
            return []
        return [
            make_finding(
                ActiveContentFinding.REMOTE_TEMPLATE,
                "Document loads its template from a remote location.",
                target=templates[0],
            )
        ]
