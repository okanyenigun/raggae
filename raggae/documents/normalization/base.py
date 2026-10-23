"""The shared interface for independently usable normalization workers."""

from typing import Protocol, runtime_checkable
from .models import NormalizationRequest, NormalizationResult


@runtime_checkable
class NormalizationWorker(Protocol):
    """Accept one request and return an artifact plus a worker-specific report.

    Concrete workers retain their precise report annotations. Successful calls
    return the changed or original artifact; failed calls return no artifact.
    """

    @property
    def name(self) -> str: ...

    def normalize(self, request: NormalizationRequest) -> NormalizationResult: ...
