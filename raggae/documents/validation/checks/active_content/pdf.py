import pikepdf
from collections.abc import Mapping
from pathlib import Path
from .base import reference_finding
from .policy import ActiveContentPolicy, ActiveContentFinding, ACTIVE_CATEGORY_MESSAGES
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome


class _MalformedAnnotationsError(ValueError):
    """Annotation metadata has a shape that cannot be safely inspected."""


class PdfActiveContentProbe:
    """
    Walks the document catalogue and page annotations for actions, attachments
    and outbound references.

    A PDF can run JavaScript when it opens, start an external program, or fetch a
    URL — none of which we will do, and all of which the next person to open it in
    Acrobat might.
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
            document = pikepdf.Pdf.open(
                target, password=password or "", attempt_recovery=False
            )
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(ActiveContentFinding.UNREADABLE, target, error),)
            )

        with document:
            seen: set[str] = set()
            references: list[str] = []
            self._inspect_catalogue(document, seen, references)
            try:
                self._inspect_pages(document, seen, references)
            except _MalformedAnnotationsError as error:
                return CheckOutcome(
                    findings=(
                        unreadable(ActiveContentFinding.UNREADABLE, target, error),
                    )
                )

        findings = [
            make_finding(code, message)
            for code, message in sorted(ACTIVE_CATEGORY_MESSAGES.items())
            if code in seen
        ]
        findings += reference_finding(references, self._policy)
        return CheckOutcome(findings=tuple(findings))

    @property
    def name(self) -> str:
        return "validation_active_content_pdf"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"pdf"})

    @property
    def policy(self) -> ActiveContentPolicy:
        return self._policy

    def _inspect_catalogue(
        self, document: pikepdf.Pdf, seen: set[str], references: list[str]
    ) -> None:
        root = document.Root
        names = root.get("/Names")

        if names is not None and "/JavaScript" in names:
            seen.add(ActiveContentFinding.JAVASCRIPT)
        if names is not None and "/EmbeddedFiles" in names:
            seen.add(ActiveContentFinding.EMBEDDED_FILE)
        if "/OpenAction" in root:
            seen.add(ActiveContentFinding.AUTO_ACTION)
            self._inspect_action(root.get("/OpenAction"), seen, references)
        if "/AA" in root:
            seen.add(ActiveContentFinding.AUTO_ACTION)
            self._inspect_additional_actions(root.get("/AA"), seen, references)

    def _inspect_pages(
        self, document: pikepdf.Pdf, seen: set[str], references: list[str]
    ) -> None:
        for number, page in enumerate(document.pages, start=1):
            if "/AA" in page:
                seen.add(ActiveContentFinding.AUTO_ACTION)
                self._inspect_additional_actions(page.get("/AA"), seen, references)
            annotations = page.get("/Annots")
            if annotations is None:
                continue
            if not isinstance(annotations, (pikepdf.Array, list, tuple)):
                raise _MalformedAnnotationsError(
                    f"Page {number} has an annotation list that is not an array"
                )
            for index, annotation in enumerate(annotations, start=1):
                if not isinstance(annotation, (pikepdf.Dictionary, Mapping)):
                    raise _MalformedAnnotationsError(
                        f"Annotation {index} on page {number} is not a dictionary"
                    )
                subtype = str(annotation.get("/Subtype", ""))
                if subtype == "/FileAttachment":
                    seen.add(ActiveContentFinding.EMBEDDED_FILE)
                elif subtype == "/RichMedia":
                    seen.add(ActiveContentFinding.RICH_MEDIA)
                self._inspect_action(annotation.get("/A"), seen, references)
                if "/AA" in annotation:
                    seen.add(ActiveContentFinding.AUTO_ACTION)
                    self._inspect_additional_actions(
                        annotation.get("/AA"), seen, references
                    )

    def _inspect_additional_actions(
        self, actions: object, seen: set[str], references: list[str]
    ) -> None:
        if not isinstance(actions, (pikepdf.Dictionary, Mapping)):
            return
        for _, action in actions.items():
            self._inspect_action(action, seen, references)

    def _inspect_action(
        self, action: object, seen: set[str], references: list[str]
    ) -> None:
        pending = [action]
        visited_indirect: set[tuple[int, int]] = set()
        # Keep direct wrappers alive: otherwise Python could reuse an old id.
        visited_direct: dict[int, object] = {}
        while pending:
            current = pending.pop()
            # An initial-page destination array is not an action dictionary.
            if not isinstance(current, (pikepdf.Dictionary, Mapping)):
                continue
            objgen = getattr(current, "objgen", (0, 0))
            if objgen != (0, 0):
                if objgen in visited_indirect:
                    continue
                visited_indirect.add(objgen)
            else:
                identity = id(current)
                if identity in visited_direct:
                    continue
                visited_direct[identity] = current

            kind = str(current.get("/S", ""))
            if kind == "/JavaScript":
                seen.add(ActiveContentFinding.JAVASCRIPT)
            elif kind == "/Launch":
                seen.add(ActiveContentFinding.LAUNCH_ACTION)
            elif kind == "/URI":
                uri = current.get("/URI")
                if uri is not None:
                    references.append(str(uri))
            elif kind == "/GoToR":
                target = current.get("/F")
                if target is not None:
                    references.append(str(target))

            following = current.get("/Next")
            if isinstance(following, (pikepdf.Array, list, tuple)):
                pending.extend(reversed(following))
            elif following is not None:
                pending.append(following)
