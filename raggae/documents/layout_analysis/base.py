"""One contract for file analyzers, independent of later layout refinements."""

from pathlib import Path
from typing import Protocol, runtime_checkable
from .models import LayoutAnalysisResult
from ..normalization.artifacts import Artifact


@runtime_checkable
class LayoutAnalyzer(Protocol):
    @property
    def name(self) -> str: ...

    def analyze(
        self, source: Path | str | bytes | Artifact
    ) -> LayoutAnalysisResult: ...
