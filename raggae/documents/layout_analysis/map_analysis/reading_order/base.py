"""Ordering operates on a document map, not on a source-file request."""

from typing import Literal, Protocol, runtime_checkable
from .models import ReadingOrderResult
from ...models import LayoutAnalysisResult


@runtime_checkable
class ReadingOrderResolver(Protocol):
    @property
    def name(self) -> str: ...

    def resolve_order(
        self,
        layout: LayoutAnalysisResult,
        *,
        direction: Literal["ltr", "rtl"] = "ltr",
    ) -> ReadingOrderResult: ...
