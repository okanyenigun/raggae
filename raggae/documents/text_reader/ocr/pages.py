"""The pages or frames of a source, drawn on demand as pictures for recognition."""

from typing import Any
from PIL import Image
from ...layout_analysis.models import ImageFrameLocation
from ...pages import Pages, PdfPages

__all__ = ["ImageFrames", "Pages", "PdfPages"]


class ImageFrames(Pages):
    """Image frames decoded by Pillow; size is the frame in pixels."""

    def __init__(self, image: Image.Image) -> None:
        self._image = image
        self.count = getattr(image, "n_frames", 1)

    def locate(self, location: Any) -> int | None:
        return (
            location.frame_number - 1
            if isinstance(location, ImageFrameLocation)
            else None
        )

    def render(self, index: int) -> tuple[Image.Image, tuple[float, float]]:
        self._image.seek(index)
        frame = self._image.convert("RGBA")
        try:
            # Composite transparency on white, as with rendered PDF paper.
            background = Image.new("RGB", frame.size, "white")
            background.paste(frame, mask=frame.getchannel("A"))
        finally:
            frame.close()
        return background, (float(background.width), float(background.height))

    def close(self) -> None:
        self._image.close()
