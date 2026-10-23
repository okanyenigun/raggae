"""Strip selected image metadata, retaining ICC colour profiles and pixel mode."""

from PIL import Image, JpegImagePlugin
from typing import Any, BinaryIO, Literal, cast, Unpack, overload
from .models import (
    ImageMetadataFinding,
    ImageMetadataIssue,
    ImageMetadataOutcome,
    ImageMetadataReport,
)
from ...io import SharedInputOutputHandler
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


# TIFF structural tags describe encoded pixels, unlike authoring/EXIF metadata.
# Resolution and ICC tags are kept for interpretation, not treated as private EXIF.
_TIFF_INTERPRETATION_TAGS = frozenset(
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
        277,
        278,
        279,
        282,
        283,
        284,
        296,
        317,
        320,
        322,
        323,
        324,
        325,
        338,
        339,
        530,
        531,
        532,
        34675,
    }
)
_PNG_XMP_KEY = "XML:com.adobe.xmp"


class ImageMetadataSanitizer:
    """Opt-in single-frame JPEG/PNG/TIFF metadata removal, not anonymity."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_image_metadata"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[ImageMetadataReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[ImageMetadataReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[ImageMetadataReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> ImageMetadataReport:
        try:
            image = Image.open(source)
        except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
            return self._failure(
                ImageMetadataFinding.DECODE_FAILED, "Image could not be decoded."
            )
        with image:
            try:
                if image.format not in {"JPEG", "PNG", "TIFF"}:
                    return self._failure(
                        ImageMetadataFinding.UNSUPPORTED_FORMAT,
                        "Only JPEG, PNG, and TIFF metadata sanitization is supported.",
                    )
                image_format = cast(Literal["JPEG", "PNG", "TIFF"], image.format)
                if getattr(image, "n_frames", 1) != 1:
                    return self._failure(
                        ImageMetadataFinding.MULTIPLE_FRAMES,
                        "Multi-frame images are not rewritten; no frames were discarded.",
                    )
                # TIFF load can consume orientation; inspect those tags beforehand.
                exif_tags = set(image.getexif())
                image.load()  # Decode strictly even when no metadata is present.
                text = getattr(image, "text", {})
                removed: list[Literal["exif", "xmp", "comment", "png_text"]] = []
                if image_format == "TIFF":
                    if exif_tags - _TIFF_INTERPRETATION_TAGS - {700}:
                        removed.append("exif")
                elif "exif" in image.info or exif_tags:
                    removed.append("exif")
                if (
                    "xmp" in image.info
                    or _PNG_XMP_KEY in text
                    or (image_format == "TIFF" and 700 in exif_tags)
                ):
                    removed.append("xmp")
                if "comment" in image.info:
                    removed.append("comment")
                if image_format == "PNG" and any(key != _PNG_XMP_KEY for key in text):
                    removed.append("png_text")
                if not removed:
                    return ImageMetadataReport(
                        outcome=ImageMetadataOutcome.NO_CHANGE,
                        image_format=image_format,
                        dimensions=image.size,
                    )
                clean = image.copy()
                clean.info.clear()
                clean.getexif().clear()
                clean.format = image_format
            except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
                return self._failure(
                    ImageMetadataFinding.DECODE_FAILED, "Image could not be decoded."
                )
            with clean:
                try:
                    options: dict[str, Any] = {}
                    for key in ("icc_profile", "dpi"):
                        if key in image.info:
                            options[key] = image.info[key]
                    if image_format == "PNG" and "transparency" in image.info:
                        options["transparency"] = image.info["transparency"]
                    if image_format == "TIFF" and "compression" in image.info:
                        options["compression"] = image.info["compression"]
                    if image_format == "JPEG":
                        jpeg_source = cast(JpegImagePlugin.JpegImageFile, image)
                        options["qtables"] = jpeg_source.quantization
                        options["subsampling"] = JpegImagePlugin.get_sampling(
                            jpeg_source
                        )
                    clean.save(output, format=image_format, **options)
                except (KeyError, TypeError, ValueError):
                    return self._failure(
                        ImageMetadataFinding.ENCODE_FAILED,
                        "Sanitized image could not be encoded.",
                    )
                # Output OSError propagates; the handler discards incomplete bytes.
                return ImageMetadataReport(
                    outcome=ImageMetadataOutcome.CHANGED,
                    image_format=image_format,
                    dimensions=clean.size,
                    removed_categories=tuple(removed),
                )

    @staticmethod
    def _failure(code: ImageMetadataFinding, message: str) -> ImageMetadataReport:
        return ImageMetadataReport(
            outcome=ImageMetadataOutcome.FAILED,
            issues=(ImageMetadataIssue(code=code, message=message),),
        )
