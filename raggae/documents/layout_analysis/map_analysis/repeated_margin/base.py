"""Repeated-margin detection is a separate job from file layout analysis."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from ....normalization.artifacts import Artifact

from ...models import LayoutAnalysisResult
from .models import RepeatedMarginResult


@runtime_checkable
class RepeatedMarginDetector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(self, layout: LayoutAnalysisResult, source: Path | str | bytes | Artifact) -> RepeatedMarginResult: ...
