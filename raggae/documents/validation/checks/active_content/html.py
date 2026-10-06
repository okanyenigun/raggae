import re
import codecs
from html.parser import HTMLParser
from pathlib import Path
from .base import reference_finding
from .policy import ActiveContentPolicy, ActiveContentFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome, Finding


class _HtmlFeatures(HTMLParser):
    _HANDLER = re.compile(r"on[a-z]+", re.IGNORECASE)
    _SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script = False
        self.frame = False
        self.handler = False
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.script = True
        if tag in {"iframe", "object", "embed"}:
            self.frame = True
        for name, value in attrs:
            if self._HANDLER.fullmatch(name):
                self.handler = True
            if name not in {"src", "href", "action"} or value is None:
                continue
            reference = value.strip()
            if reference.startswith("//"):
                self.references.append(f"https:{reference}")
            elif self._SCHEME.match(reference):
                self.references.append(reference)


class HtmlActiveContentProbe:
    """Inspects actual tags/attributes, without executing or fetching content."""

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
            with target.open("rb") as stream:
                prefix = stream.read(3)
            if prefix.startswith(codecs.BOM_UTF8):
                encoding = "utf-8-sig"
            elif prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                encoding = "utf-16"
            else:
                encoding = "utf-8"
            content = target.read_text(encoding=encoding, errors="replace")
        except OSError as error:
            return CheckOutcome(
                findings=(unreadable(ActiveContentFinding.UNREADABLE, target, error),)
            )

        parsed = _HtmlFeatures()
        parsed.feed(content)
        parsed.close()
        findings: list[Finding] = []
        if parsed.script:
            findings.append(
                make_finding(ActiveContentFinding.SCRIPT, "Document contains a script.")
            )
        if parsed.frame:
            findings.append(
                make_finding(
                    ActiveContentFinding.EMBEDDED_FRAME,
                    "Document embeds another document or object.",
                )
            )
        if parsed.handler:
            findings.append(
                make_finding(
                    ActiveContentFinding.EVENT_HANDLER,
                    "Document contains an inline event handler.",
                )
            )
        findings += reference_finding(parsed.references, self._policy)
        return CheckOutcome(findings=tuple(findings))

    @property
    def name(self) -> str:
        return "validation_active_content_html"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"html"})

    @property
    def policy(self) -> ActiveContentPolicy:
        return self._policy

    def _references(self, content: str) -> list[str]:
        parsed = _HtmlFeatures()
        parsed.feed(content)
        parsed.close()
        return parsed.references
