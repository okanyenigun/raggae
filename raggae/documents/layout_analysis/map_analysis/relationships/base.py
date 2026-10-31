"""Relationship workers inspect a map; they do not choose or run file analyzers."""

from typing import Protocol, runtime_checkable
from .models import RegionRelationshipResult
from ...models import LayoutAnalysisResult


@runtime_checkable
class RegionRelationshipDetector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(self, layout: LayoutAnalysisResult) -> RegionRelationshipResult: ...
