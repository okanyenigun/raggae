from pathlib import Path
from .policy import TextLimits, ResourceFinding
from ..utils import (
    require_path,
    applicability,
    normalize_extension,
    unreadable,
    make_finding,
)
from ...schemas.result import CheckOutcome, FactValue, Finding
from ...shared import TEXT_FORMATS


class TextResourceProbe:
    """
    Nesting depth for json and jsonl. The rest of the text family has nothing to
    measure here — its size is already bounded by use case 2.

    Depth is counted by scanning brackets, not by parsing. Parsing is what the
    limit exists to protect: ``json.loads`` on deeply nested input exhausts the
    interpreter's stack before any limit could be applied.
    """

    def __init__(self, policy: TextLimits | None = None) -> None:
        self._policy = policy or TextLimits()

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

        fmt = normalize_extension(detected_format) if detected_format else None
        if fmt not in {"json", "jsonl"}:
            # Nothing this format declares can cost anything here.
            return CheckOutcome()

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return CheckOutcome(
                findings=(unreadable(ResourceFinding.UNREADABLE, target, error),)
            )

        depth = self._deepest_nesting(content)
        facts: dict[str, FactValue] = {"nesting_depth": depth}
        return CheckOutcome(findings=tuple(self._check_depth(depth)), facts=facts)

    @property
    def name(self) -> str:
        return "validation_resource_limits_text"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset(TEXT_FORMATS)

    @property
    def policy(self) -> TextLimits:
        return self._policy

    @staticmethod
    def _deepest_nesting(content: str) -> int:
        depth = 0
        deepest = 0
        in_string = False
        escaped = False

        for character in content:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character in "{[":
                depth += 1
                deepest = max(deepest, depth)
            elif character in "}]":
                depth = max(0, depth - 1)
        return deepest

    def _check_depth(self, depth: int) -> list[Finding]:
        limit = self._policy.max_nesting_depth
        if depth <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.NESTING_TOO_DEEP,
                "Document nests more deeply than the limit allows.",
                limit=limit,
                observed=depth,
            )
        ]
