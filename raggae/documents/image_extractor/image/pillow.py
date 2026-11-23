"""Pictures from PNG, JPEG, and TIFF files with Pillow: frames, or crops of regions."""

from collections.abc import Sequence
from io import BytesIO
from math import isclose
from pathlib import Path
from stat import S_ISREG
from typing import Any
from PIL import Image, UnidentifiedImageError
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
    ImageMediaType,
)
from ..utils import (
    encode,
    failure,
    finding,
    nothing_selected,
    select_image_regions,
    too_many_pixels,
)
from ...layout_analysis.models import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from ...normalization.artifacts import Artifact
from ...sources import as_artifact

# Pillow format names this worker accepts; MPO is a JPEG with a multi-picture marker.
_FORMATS: dict[str, ImageMediaType] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "TIFF": "image/tiff",
}
_DECODE_ERRORS = (OSError, ValueError, SyntaxError, EOFError)


class PillowImageExtractor:
    """Return the frames of a PNG, JPEG, or TIFF file, or crops of its layout regions.

    Without a layout: one picture per frame. A single-frame file comes back
    byte for byte (``original``); each frame of a file with several (a
    multi-page TIFF, an animated PNG) is saved as a new file (``reencoded``).

    With a layout: a crop of each requested region on its frame, carrying its
    ``region_id``. Without ``region_ids`` only image regions are cropped;
    explicit ``region_ids`` are cropped whatever their kind. Overlapping regions
    are allowed. Crops keep their pixels, transparency included, and are new
    files (``reencoded``).

    New files are PNG, or TIFF when PNG cannot hold the pixels (CMYK). Pixel
    coordinates follow Pillow's decoding, as layout analysis does. A photo's
    EXIF rotation is not applied: turning pixels upright is normalization's job
    (``PillowImageOrientationNormalizer``). Other formats are not applicable.
    Frames above Pillow's decompression-bomb ceiling are skipped with a finding.
    """

    _INFORMATIONAL = frozenset({ImageFindingCode.NOTHING_SELECTED})

    @property
    def name(self) -> str:
        return "image_extractor_pillow"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult:
        artifact = as_artifact(source)
        selected = select_image_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name, explicit=region_ids is not None)
        try:
            if artifact.path is not None and not S_ISREG(artifact.path.stat().st_mode):
                return self._failure(
                    ImageFindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = artifact.read_bytes()
        except OSError:
            return self._failure(
                ImageFindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(ImageFindingCode.DECODE_FAILED, "Source is empty.")
        try:
            image = Image.open(BytesIO(data))
        except Image.DecompressionBombError:
            return self._failure(
                ImageFindingCode.IMAGE_TOO_LARGE,
                "The image is above Pillow's decompression-bomb limit.",
            )
        except (UnidentifiedImageError, *_DECODE_ERRORS):
            return self._not_applicable()
        with image:
            media_type = _FORMATS.get(image.format or "")
            if media_type is None:
                return self._not_applicable()
            frames = getattr(image, "n_frames", 1)
            if selected is None:
                return self._frames(image, data, media_type, frames)
            surfaces = {} if layout is None else {s.id_: s for s in layout.surfaces}
            return self._regions(image, frames, selected, surfaces)

    def _frames(
        self, image: Any, data: bytes, media_type: ImageMediaType, frames: int
    ) -> ImageExtractResult:
        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        for index in range(frames):
            try:
                image.seek(index)
                if too_many_pixels(*image.size):
                    findings.append(PillowImageExtractor._too_large(index))
                    continue
                image.load()
                if frames == 1:
                    picture, kind = data, media_type
                else:
                    picture, kind = encode(image)
            except Image.DecompressionBombError:
                findings.append(PillowImageExtractor._too_large(index))
                continue
            except _DECODE_ERRORS:
                findings.append(PillowImageExtractor._unreadable(index))
                continue
            images.append(
                ExtractedImage(
                    data=picture,
                    media_type=kind,
                    width=image.width,
                    height=image.height,
                    page_index=index,
                    method="original" if frames == 1 else "reencoded",
                )
            )
        return self._result(images, findings, len(images))

    def _regions(
        self,
        image: Any,
        frames: int,
        selected: tuple[LayoutRegion, ...],
        surfaces: dict[str, LayoutSurface],
    ) -> ImageExtractResult:
        findings: list[ImageExtractFinding] = []
        pending: dict[int, list[LayoutRegion]] = {}
        for region in selected:
            location = surfaces[region.surface_id].location
            if not isinstance(location, ImageFrameLocation) or region.bbox is None:
                findings.append(
                    ImageExtractFinding(
                        code=ImageFindingCode.SELECTION_UNSUPPORTED,
                        message="Selection is not a region on an image frame.",
                        region_id=region.id,
                    )
                )
            elif location.frame_number > frames:
                findings.append(
                    finding(
                        ImageFindingCode.PAGE_MISSING,
                        "Requested frame does not exist in this image.",
                        location.frame_number - 1,
                        region.id,
                    )
                )
            else:
                pending.setdefault(location.frame_number - 1, []).append(region)

        images: list[ExtractedImage] = []
        for index in sorted(pending):
            regions = pending[index]
            try:
                image.seek(index)
                if too_many_pixels(*image.size):
                    findings.extend(
                        PillowImageExtractor._too_large(index, r.id) for r in regions
                    )
                    continue
                image.load()
            except Image.DecompressionBombError:
                findings.extend(
                    PillowImageExtractor._too_large(index, r.id) for r in regions
                )
                continue
            except _DECODE_ERRORS:
                findings.extend(
                    PillowImageExtractor._unreadable(index, r.id) for r in regions
                )
                continue
            for region in regions:
                surface = surfaces[region.surface_id]
                if not PillowImageExtractor._fits(surface, image.size):
                    findings.append(
                        finding(
                            ImageFindingCode.LAYOUT_MISMATCH,
                            "Layout dimensions do not match the source frame.",
                            index,
                            region.id,
                        )
                    )
                    continue
                images.append(PillowImageExtractor._crop(image, index, region))
        return self._result(images, findings, len(images))

    @staticmethod
    def _crop(image: Any, index: int, region: LayoutRegion) -> ExtractedImage:
        """Every pixel the region touches, saved as a new file."""
        assert region.bbox is not None
        left, top, right, bottom = region.bbox
        x0 = min(max(0, int(left)), image.width - 1)
        y0 = min(max(0, int(top)), image.height - 1)
        x1 = min(image.width, max(x0 + 1, int(right + 0.999)))
        y1 = min(image.height, max(y0 + 1, int(bottom + 0.999)))
        with image.crop((x0, y0, x1, y1)) as piece:
            data, media_type = encode(piece)
            return ExtractedImage(
                data=data,
                media_type=media_type,
                width=piece.width,
                height=piece.height,
                page_index=index,
                region_id=region.id,
                method="reencoded",
            )

    @staticmethod
    def _fits(surface: LayoutSurface, size: tuple[int, int]) -> bool:
        return (
            surface.width is not None
            and surface.height is not None
            and isclose(surface.width, size[0], abs_tol=0.01)
            and isclose(surface.height, size[1], abs_tol=0.01)
        )

    def _result(
        self,
        images: list[ExtractedImage],
        findings: list[ImageExtractFinding],
        completed: int,
    ) -> ImageExtractResult:
        incomplete = any(
            f.code not in PillowImageExtractor._INFORMATIONAL for f in findings
        )
        return ImageExtractResult(
            extractor=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            images=tuple(images),
            findings=tuple(findings),
        )

    def _not_applicable(self) -> ImageExtractResult:
        return ImageExtractResult(
            extractor=self.name,
            outcome="not_applicable",
            findings=(
                ImageExtractFinding(
                    code=ImageFindingCode.NOT_APPLICABLE,
                    message="Source is not a PNG, JPEG, or TIFF image.",
                ),
            ),
        )

    def _failure(self, code: ImageFindingCode, message: str) -> ImageExtractResult:
        return failure(self.name, code, message)

    @staticmethod
    def _too_large(index: int, region_id: str | None = None) -> ImageExtractFinding:
        return finding(
            ImageFindingCode.IMAGE_TOO_LARGE,
            "This frame is above Pillow's decompression-bomb limit and was skipped.",
            index,
            region_id,
        )

    @staticmethod
    def _unreadable(index: int, region_id: str | None = None) -> ImageExtractFinding:
        return finding(
            ImageFindingCode.PAGE_UNREADABLE,
            "This frame could not be decoded.",
            index,
            region_id,
        )
