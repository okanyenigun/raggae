"""Ordered worker reports for one normalization chain."""

from typing import Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SerializeAsAny


class NormalizationStepReport(BaseModel):
    """Keep a worker's name and its own report, without document bytes."""

    model_config = ConfigDict(frozen=True)

    worker: str
    report: SerializeAsAny[BaseModel]
    output_path: Path | None = None


class DocumentNormalizationReport(BaseModel):
    """Overall outcome and reports for executed workers, in execution order."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal["changed", "no_change", "failed"]
    steps: tuple[NormalizationStepReport, ...] = ()
