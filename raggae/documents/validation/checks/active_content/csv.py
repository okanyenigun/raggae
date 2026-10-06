import csv
import codecs
from io import StringIO
from pathlib import Path
from .policy import ActiveContentPolicy, ActiveContentFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome


class CsvActiveContentProbe:
    """
    Finds cells that a spreadsheet would treat as formulas.

    CSV has no formulas — it is plain text — so a cell beginning ``=`` was put
    there to execute when someone opens the file in Excel. The same cell inside an
    ``.xlsx`` is unremarkable, which is why this probe covers only CSV.
    """

    _PREFIXES = ("=", "+", "-", "@")

    def __init__(self, policy: ActiveContentPolicy | None = None) -> None:
        self._policy = policy or ActiveContentPolicy()
        self._referenced_samples = 5

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

        offenders = self._formula_cells(content)
        if not offenders:
            return CheckOutcome()
        return CheckOutcome(
            findings=(
                make_finding(
                    ActiveContentFinding.SPREADSHEET_FORMULA,
                    "Cells would be evaluated as formulas by a spreadsheet.",
                    count=len(offenders),
                    sample=", ".join(offenders[: self._referenced_samples]),
                ),
            )
        )

    @property
    def name(self) -> str:
        return "validation_active_content_csv"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"csv"})

    @property
    def policy(self) -> ActiveContentPolicy:
        return self._policy

    def set_referenced_samples(self, value: int) -> None:
        self._referenced_samples = value

    @classmethod
    def _formula_cells(cls, content: str) -> list[str]:
        found: list[str] = []
        for row in csv.reader(StringIO(content, newline="")):
            for cell in row:
                value = cell.strip()
                if value.startswith(cls._PREFIXES) and not cls._is_number(value):
                    found.append(value[:60])
        return found

    @staticmethod
    def _is_number(value: str) -> bool:
        """
        A negative number is not a formula.

        ``-`` and ``+`` start far more numbers than payloads, and reporting every
        negative figure in a spreadsheet would bury the one cell that matters.
        """
        try:
            float(value)
        except ValueError:
            return False
        return True
