from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit
from enum import StrEnum
from ...schemas.result import Severity


class ActiveContentFinding(StrEnum):
    """What active content inspection can report, across every format."""

    NOT_APPLICABLE = "active_content.not_applicable"
    UNREADABLE = "active_content.unreadable"

    JAVASCRIPT = "active_content.javascript"
    AUTO_ACTION = "active_content.auto_action"
    LAUNCH_ACTION = "active_content.launch_action"
    EMBEDDED_FILE = "active_content.embedded_file"
    RICH_MEDIA = "active_content.rich_media"

    MACRO = "active_content.macro"
    REMOTE_TEMPLATE = "active_content.remote_template"

    SCRIPT = "active_content.script"
    EMBEDDED_FRAME = "active_content.embedded_frame"
    EVENT_HANDLER = "active_content.event_handler"

    SPREADSHEET_FORMULA = "active_content.spreadsheet_formula"

    REMOTE_REFERENCE = "active_content.remote_reference"


class ActiveContentPolicy(BaseModel):
    """Which outbound references are unremarkable enough not to report."""

    model_config = ConfigDict(frozen=True)

    allowed_reference_hosts: frozenset[str] = Field(
        default=frozenset(),
        description=(
            "Hosts whose remote references are not reported. A parent domain "
            "covers its subdomains. Empty reports everything."
        ),
    )
    severities: dict[str, Severity] = Field(
        default_factory=lambda: {
            ActiveContentFinding.UNREADABLE: Severity.REJECT,
            # The exception. No document needs to start a program.
            ActiveContentFinding.LAUNCH_ACTION: Severity.REJECT,
            ActiveContentFinding.JAVASCRIPT: Severity.WARNING,
            ActiveContentFinding.AUTO_ACTION: Severity.WARNING,
            ActiveContentFinding.EMBEDDED_FILE: Severity.WARNING,
            ActiveContentFinding.RICH_MEDIA: Severity.WARNING,
            ActiveContentFinding.MACRO: Severity.WARNING,
            ActiveContentFinding.REMOTE_TEMPLATE: Severity.WARNING,
            ActiveContentFinding.SCRIPT: Severity.WARNING,
            ActiveContentFinding.EMBEDDED_FRAME: Severity.WARNING,
            ActiveContentFinding.EVENT_HANDLER: Severity.WARNING,
            ActiveContentFinding.SPREADSHEET_FORMULA: Severity.WARNING,
            # A document containing a link is not remarkable.
            ActiveContentFinding.REMOTE_REFERENCE: Severity.INFO,
            ActiveContentFinding.NOT_APPLICABLE: Severity.INFO,
        },
        description="Default decision severity for each active-content finding.",
    )

    @field_validator("allowed_reference_hosts", mode="after")
    @classmethod
    def _normalize(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(
            host.strip().strip(".").casefold() for host in value if host.strip(" .")
        )

    def allows(self, reference: str) -> bool:
        """
        Whether *reference* points somewhere already accounted for.

        Matches the parsed host, not the URL text. The host is the only part that
        decides who learns the document was opened, and parsing is what keeps
        ``https://evil.com/?ref=cdn.allowed.com`` from passing as allowed.

        A reference with no host — ``mailto:``, ``file:``, ``javascript:`` — is
        never allowed, because it is not the kind of thing this list is about.
        """
        if not self.allowed_reference_hosts:
            return False
        try:
            host = (urlsplit(reference).hostname or "").casefold()
        except ValueError:
            return False
        if not host:
            return False
        return any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in self.allowed_reference_hosts
        )


ACTIVE_CATEGORY_MESSAGES: dict[str, str] = {
    ActiveContentFinding.JAVASCRIPT: "Document contains JavaScript.",
    ActiveContentFinding.AUTO_ACTION: "Document runs an action when it is opened.",
    ActiveContentFinding.LAUNCH_ACTION: "Document can start an external program.",
    ActiveContentFinding.EMBEDDED_FILE: "Document carries an embedded file.",
    ActiveContentFinding.RICH_MEDIA: "Document embeds rich media.",
}
