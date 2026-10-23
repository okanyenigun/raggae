"""Convert one image to one PDF page without selecting a worker chain."""

import math
from io import BytesIO
from numbers import Real
from PIL import Image
from typing import BinaryIO, Literal, cast, Unpack, overload
from .models import (
    ImageToPdfFinding,
    ImageToPdfIssue,
    ImageToPdfOutcome,
    ImageToPdfReport,
)
from ..io import SharedInputOutputHandler
from ..models import NormalizationRequest, NormalizationResult
from ..request import NormalizationArguments, resolve_request


class PillowImageToPdfConverter:
    """JPEG/PNG/TIFF to a single RGB PDF page, with white behind transparency."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_pillow_image_to_pdf"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[ImageToPdfReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[ImageToPdfReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[ImageToPdfReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(report=report, artifact=artifact)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> ImageToPdfReport:
        try:
            image = Image.open(source)
        except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
            return self._failure(
                ImageToPdfFinding.DECODE_FAILED, "Image could not be decoded."
            )

        with image:
            try:
                if image.format not in {"JPEG", "PNG", "TIFF"}:
                    return self._failure(
                        ImageToPdfFinding.UNSUPPORTED_FORMAT,
                        "Only JPEG, PNG, and TIFF conversion is supported.",
                    )
                image_format = cast(Literal["JPEG", "PNG", "TIFF"], image.format)
                if getattr(image, "n_frames", 1) != 1:
                    return self._failure(
                        ImageToPdfFinding.MULTIPLE_FRAMES,
                        "Multi-frame images are not converted; no frames were discarded.",
                    )
                image.load()
                dpi = self._resolution(image)
                flattened = "A" in image.getbands() or "transparency" in image.info
                rgb = self._to_rgb(image, flattened)
            except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
                return self._failure(
                    ImageToPdfFinding.DECODE_FAILED, "Image could not be decoded."
                )

            with rgb, BytesIO() as encoded:
                # No source metadata propagation, especially anisotropic DPI
                # that could override our uniform resolution and stretch the page.
                rgb.info.clear()
                try:
                    rgb.save(encoded, format="PDF", resolution=dpi)
                except (OSError, KeyError, TypeError, ValueError):
                    return self._failure(
                        ImageToPdfFinding.ENCODE_FAILED,
                        "Image could not be encoded as PDF.",
                    )
                data = encoded.getvalue()

            # Destination failures must propagate, not become encoding findings.
            offset = 0
            while offset < len(data):
                written = output.write(data[offset:])
                if written is None or written <= 0:
                    raise OSError("destination did not accept PDF bytes")
                offset += written
            return ImageToPdfReport(
                outcome=ImageToPdfOutcome.CHANGED,
                image_format=image_format,
                dimensions=image.size,
                resolution_dpi=dpi,
                transparency_flattened=flattened,
            )

    @staticmethod
    def _resolution(image: Image.Image) -> float:
        value = image.info.get("dpi")
        if isinstance(value, (tuple, list)):
            value = value[0] if value else None
        # TIFF exposes resolution as IFDRational, a real number rather than
        # an int/float. Still reject strings and booleans as metadata values.
        if isinstance(value, Real) and not isinstance(value, bool):
            try:
                dpi = float(value)
                if (
                    math.isfinite(dpi)
                    and dpi > 0
                    and all(
                        math.isfinite(dimension * 72.0 / dpi)
                        for dimension in image.size
                    )
                ):
                    return dpi
            except OverflowError:
                pass
        return 150.0

    @staticmethod
    def _to_rgb(image: Image.Image, flattened: bool) -> Image.Image:
        if not flattened:
            return image.convert("RGB")
        with image.convert("RGBA") as rgba, rgba.getchannel("A") as alpha:
            rgb = Image.new("RGB", image.size, "white")
            rgb.paste(rgba, mask=alpha)
            return rgb

    @staticmethod
    def _failure(code: ImageToPdfFinding, message: str) -> ImageToPdfReport:
        return ImageToPdfReport(
            outcome=ImageToPdfOutcome.FAILED,
            issues=(ImageToPdfIssue(code=code, message=message),),
        )
