"""Apply EXIF orientation to pixels, retaining the detected image format."""

from PIL import Image, ImageOps, PngImagePlugin, TiffImagePlugin
from typing import Any, BinaryIO, Literal, cast, Unpack, overload
from .models import (
    ImageOrientationFinding,
    ImageOrientationIssue,
    ImageOrientationOutcome,
    ImageOrientationReport,
    _OPERATIONS,
)
from ..io import SharedInputOutputHandler
from ..models import NormalizationRequest, NormalizationResult
from ..request import NormalizationArguments, resolve_request


# Pixel layout and encoded offsets must be regenerated after transforming pixels.
_TIFF_STRUCTURAL_TAGS = frozenset(
    {
        254,
        256,
        257,
        258,
        259,
        262,
        263,
        264,
        265,
        266,
        273,
        274,
        277,
        278,
        279,
        284,
        317,
        322,
        323,
        324,
        325,
        338,
        339,
        530,
        531,
        532,
    }
)


class PillowImageOrientationNormalizer:
    """Normalize single-frame JPEG, PNG, or TIFF; delegate storage to the handler."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_pillow_image_orientation"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[ImageOrientationReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[ImageOrientationReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[ImageOrientationReport]:
        request = resolve_request(request, kwargs)

        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(report=report, artifact=artifact)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> ImageOrientationReport:
        try:
            image = Image.open(source)
        except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
            return self._failure(
                ImageOrientationFinding.DECODE_FAILED, "Image could not be decoded."
            )

        with image:
            try:
                if image.format not in {"JPEG", "PNG", "TIFF"}:
                    return self._failure(
                        ImageOrientationFinding.UNSUPPORTED_FORMAT,
                        "Only JPEG, PNG, and TIFF orientation is supported.",
                    )
                image_format = cast(Literal["JPEG", "PNG", "TIFF"], image.format)
                if getattr(image, "n_frames", 1) != 1:
                    return self._failure(
                        ImageOrientationFinding.MULTIPLE_FRAMES,
                        "Multi-frame images are not rewritten; no frames were discarded.",
                    )
                dimensions = image.size
                if image_format == "TIFF":
                    # The decoder may expose the post-orientation size already.
                    tiff_source = cast(TiffImagePlugin.TiffImageFile, image)
                    dimensions = (tiff_source.tag_v2[256], tiff_source.tag_v2[257])
                orientation = image.getexif().get(274)
                if orientation is not None and (
                    not isinstance(orientation, int)
                    or isinstance(orientation, bool)
                    or orientation not in range(1, 9)
                ):
                    return self._failure(
                        ImageOrientationFinding.INVALID_ORIENTATION,
                        "EXIF orientation must be an integer from 1 through 8.",
                    )
                # Load even for no-change: malformed pixel data is not a success.
                image.load()
                if orientation in (None, 1):
                    return ImageOrientationReport(
                        outcome=ImageOrientationOutcome.NO_CHANGE,
                        image_format=image_format,
                        source_orientation=orientation,
                        input_dimensions=dimensions,
                    )
                # Pillow applies TIFF orientation during decoding: do not do it twice.
                normalized = (
                    image.copy()
                    if image_format == "TIFF"
                    else ImageOps.exif_transpose(image)
                )
            except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
                return self._failure(
                    ImageOrientationFinding.DECODE_FAILED, "Image could not be decoded."
                )

            with normalized:
                normalized.format = image_format
                try:
                    options = self._save_options(normalized, image, image_format)
                    normalized.save(output, format=image_format, **options)
                except (KeyError, TypeError, ValueError):
                    return self._failure(
                        ImageOrientationFinding.ENCODE_FAILED,
                        "Normalized image could not be encoded.",
                    )
                # Destination OSError propagates; the handler cleans incomplete output.
                return ImageOrientationReport(
                    outcome=ImageOrientationOutcome.CHANGED,
                    image_format=image_format,
                    source_orientation=orientation,
                    applied_operation=_OPERATIONS[orientation],
                    input_dimensions=dimensions,
                    output_dimensions=normalized.size,
                )

    @staticmethod
    def _save_options(
        normalized: Image.Image, source: Image.Image, image_format: str
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            key: source.info[key]
            for key in ("icc_profile", "dpi")
            if key in source.info
        }
        if image_format in {"JPEG", "PNG"} and normalized.info.get("exif"):
            options["exif"] = normalized.info["exif"]
        if image_format == "JPEG":
            options.update(quality="keep", subsampling="keep", qtables="keep")
            if "comment" in source.info:
                options["comment"] = source.info["comment"]
        elif image_format == "PNG":
            text = getattr(source, "text", {})
            if text:
                metadata = PngImagePlugin.PngInfo()
                for key, value in text.items():
                    if isinstance(value, str):
                        metadata.add_text(key, value)
                options["pnginfo"] = metadata
            if "transparency" in source.info:
                options["transparency"] = source.info["transparency"]
        else:
            metadata = TiffImagePlugin.ImageFileDirectory_v2()
            for tag, value in cast(
                TiffImagePlugin.TiffImageFile, source
            ).tag_v2.items():
                if tag not in _TIFF_STRUCTURAL_TAGS:
                    metadata[tag] = value
            options["tiffinfo"] = metadata
            if "compression" in source.info:
                options["compression"] = source.info["compression"]
        return options

    @staticmethod
    def _failure(code: ImageOrientationFinding, message: str) -> ImageOrientationReport:
        return ImageOrientationReport(
            outcome=ImageOrientationOutcome.FAILED,
            issues=(ImageOrientationIssue(code=code, message=message),),
        )
