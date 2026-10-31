"""The small boundary between file handling and local visual inference."""

from PIL import Image
from pydantic import BaseModel, ConfigDict, model_validator
from typing import Protocol, Self, runtime_checkable
from ...models import BoundingBox, RegionKind


class LayoutDetection(BaseModel):
    """One model prediction in input-image pixels, measured from top-left.

    Boxes may cross the image edge; the worker clips them to the surface.
    Predictions are candidates, not confirmed labels or a reading sequence.
    """

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    kind: RegionKind
    bbox: BoundingBox

    @model_validator(mode="after")
    def _box(self) -> Self:
        left, top, right, bottom = self.bbox
        if right <= left or bottom <= top:
            raise ValueError("detection bbox must have positive width and height")
        return self


@runtime_checkable
class LayoutDetectionService(Protocol):
    def detect(self, image: Image.Image) -> tuple[LayoutDetection, ...]:
        """Inspect one RGB image synchronously; do not close or retain it.

        Return boxes in this image's original pixel size, even if the backend
        resizes internally. Empty predictions mean no detected regions, not
        proof of a blank page. Failures must raise, never masquerade as empty.
        """
        ...
