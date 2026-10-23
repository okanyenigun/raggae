"""CSV-specific cell change facts, using the shared normalization container."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from enum import StrEnum
from typing import Self


class CsvFormulaOutcome(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    FAILED = "failed"


class CsvFormulaFinding(StrEnum):
    DECODE_FAILED = "normalization.sanitization.decode_failed"
    ENCODE_FAILED = "normalization.sanitization.encode_failed"


class CsvFormulaIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: CsvFormulaFinding
    message: str = Field(min_length=1, pattern=r"\S")


class CsvFormulaReport(BaseModel):
    """Count modified cell values, without storing their content."""

    model_config = ConfigDict(frozen=True)

    outcome: CsvFormulaOutcome
    changed_cells: int = Field(default=0, ge=0, strict=True)
    issues: tuple[CsvFormulaIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome is CsvFormulaOutcome.FAILED:
            if not self.issues:
                raise ValueError("failed requires an issue")
        elif self.issues:
            raise ValueError("only failed may contain issues")
        if self.outcome is CsvFormulaOutcome.CHANGED:
            if self.changed_cells == 0:
                raise ValueError("changed requires a positive changed_cells count")
        elif self.changed_cells != 0:
            raise ValueError("only changed may report completed cell changes")
        return self
