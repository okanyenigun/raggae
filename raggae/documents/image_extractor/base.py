"""One interface for standalone and layout-guided image extractors."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable
from .models import ImageExtractResult
from ..layout_analysis.models import LayoutAnalysisResult
from ..normalization.artifacts import Artifact


@runtime_checkable
class ImageExtractor(Protocol):
    @property
    def name(self) -> str: ...

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult: ...
