import warnings
from pathlib import Path
from PIL import Image
from .policy import ImageLimits, ResourceFinding
from ..utils import (
    require_path,
    applicability,
    pillow_limit_lifted,
    unreadable,
    make_finding,
)
from ...schemas.result import CheckOutcome, FactValue, Finding


class ImageResourceProbe:
    """
    Dimensions and frame count, read from the header. No pixels are decoded.

    This is the whole point for images: a 40 KB file can declare 60,000 × 60,000
    pixels, which costs roughly 14 GB to decode. The declaration is in the header,
    so refusing it costs a header read.
    """

    def __init__(self, policy: ImageLimits | None = None) -> None:
        self._policy = policy or ImageLimits()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)
        skipped = applicability(
            self.handles, detected_format, self.name, ResourceFinding.NOT_APPLICABLE
        )
        if skipped:
            return CheckOutcome(findings=tuple(skipped))

        try:
            with warnings.catch_warnings(), pillow_limit_lifted():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(target) as image:
                    width, height = image.size
                    frames = int(getattr(image, "n_frames", 1))
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(ResourceFinding.UNREADABLE, target, error),)
            )
        pixels = width * height
        facts: dict[str, FactValue] = {
            "image_width": width,
            "image_height": height,
            "image_pixels": pixels,
            "frame_count": frames,
        }

        findings: list[Finding] = []
        findings += self._check_pixels(pixels, width, height)
        findings += self._check_frames(frames)
        return CheckOutcome(findings=tuple(findings), facts=facts)

    def _check_pixels(self, pixels: int, width: int, height: int) -> list[Finding]:
        limit = self._policy.max_pixels
        if pixels <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.TOO_MANY_PIXELS,
                f"Image declares {width} × {height}, more pixels than the limit "
                "allows.",
                limit=limit,
                observed=pixels,
            )
        ]

    def _check_frames(self, frames: int) -> list[Finding]:
        limit = self._policy.max_frames
        if frames <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.TOO_MANY_FRAMES,
                "Image holds more frames than the limit allows.",
                limit=limit,
                observed=frames,
            )
        ]

    @property
    def name(self) -> str:
        return "validation_resource_limits_image"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"png", "jpeg", "tiff"})
