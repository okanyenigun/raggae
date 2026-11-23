"""Recover stored PDF pictures page by page with pikepdf; whole pages only."""

import warnings
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from PIL import Image, UnidentifiedImageError
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    failure,
    finding,
    image_file,
    prepare_source,
    too_large,
    too_many_pixels,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class PikepdfImageExtractor:
    """Return each stored picture once for every page that lists it, using pikepdf.

    A page's pictures are the image objects in its resources, including those
    inside nested forms (pikepdf's ``get_images``). The same picture on ten pages
    comes back ten times; one listed but never drawn still comes back; one drawn
    twice on a page comes back once. Stencil masks are not listed by pikepdf.
    Within a page, pictures follow pikepdf's listing, sorted by resource name;
    that is neither drawing nor reading order.

    A stored JPEG comes back byte for byte (``original``). Other pictures are
    decoded and saved as new files (``reencoded``): PNG, or TIFF for CMYK and
    fax-compressed pictures. pikepdf applies soft, stencil, and colour-key masks,
    so masked pictures come back as PNG with transparency. Pictures pikepdf cannot
    turn into pixels (JBIG2 without jbig2dec, Separation or DeviceN colour)
    become findings, as do pictures above Pillow's decompression-bomb ceiling.

    Inline images (pictures written into drawing commands) are not extracted;
    pages that have them get a finding. Layout-region requests are unsupported;
    a region renderer handles those. Passwords come from ``Artifact``.
    """

    _INFORMATIONAL = frozenset(
        {ImageFindingCode.EMPTY, ImageFindingCode.INLINE_IMAGES_SKIPPED}
    )

    @property
    def name(self) -> str:
        return "image_extractor_pikepdf"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult:
        prepared = prepare_source(
            source,
            extractor=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message=(
                "PikepdfImageExtractor returns stored pictures by page and cannot "
                "work from layout regions; use PdfiumImageExtractor for those."
            ),
        )
        if isinstance(prepared, ImageExtractResult):
            return prepared
        try:
            if prepared.path is not None and not S_ISREG(prepared.path.stat().st_mode):
                return self._failure(
                    ImageFindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = prepared.read_bytes()
        except OSError:
            return self._failure(
                ImageFindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(ImageFindingCode.DECODE_FAILED, "Source is empty.")
        if b"%PDF-" not in data[:1024]:
            return ImageExtractResult(
                extractor=self.name,
                outcome="not_applicable",
                findings=(
                    ImageExtractFinding(
                        code=ImageFindingCode.NOT_APPLICABLE,
                        message="Source has no PDF header.",
                    ),
                ),
            )

        import pikepdf

        errors: tuple[type[BaseException], ...] = (
            pikepdf.PdfError,
            pikepdf.DataDecodingError,
            pikepdf.InvalidPdfImageError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            AssertionError,
            ZeroDivisionError,
            OverflowError,
            RecursionError,
            SyntaxError,
            OSError,
        )
        password = prepared.password
        try:
            with warnings.catch_warnings():
                # An Artifact may carry a password for a PDF that needs none.
                warnings.filterwarnings(
                    "ignore",
                    message="A password was provided, but no password was needed",
                    category=UserWarning,
                )
                document = pikepdf.open(BytesIO(data), password=password or "")
        except pikepdf.PasswordError:
            code = (
                ImageFindingCode.INVALID_PASSWORD
                if password
                else ImageFindingCode.PASSWORD_REQUIRED
            )
            return self._failure(
                code, "PDF could not be opened with the supplied password."
            )
        except errors:
            return self._failure(
                ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
            )

        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        completed = found = 0
        with document:
            if not len(document.pages):
                return self._failure(
                    ImageFindingCode.NO_PAGES, "PDF contains no usable pages."
                )
            for index, page in enumerate(document.pages):
                try:
                    pictures = PikepdfImageExtractor._pictures(page)
                except errors:
                    findings.append(
                        finding(
                            ImageFindingCode.PAGE_UNREADABLE,
                            "The pictures on this PDF page could not be listed.",
                            index,
                        )
                    )
                    continue
                completed += 1
                found += len(pictures)
                try:
                    inline = PikepdfImageExtractor._has_inline_images(pikepdf, page)
                except errors:
                    findings.append(
                        finding(
                            ImageFindingCode.INLINE_IMAGES_SKIPPED,
                            "This page's drawing commands could not be checked for "
                            "inline images, which this extractor does not return.",
                            index,
                        )
                    )
                else:
                    if inline:
                        findings.append(
                            finding(
                                ImageFindingCode.INLINE_IMAGES_SKIPPED,
                                "This page draws inline images, which this "
                                "extractor does not return.",
                                index,
                            )
                        )
                for picture in pictures:
                    extracted = PikepdfImageExtractor._extract_one(
                        pikepdf, picture, index, errors
                    )
                    if isinstance(extracted, ExtractedImage):
                        images.append(extracted)
                    else:
                        findings.append(extracted)
        if completed and not found:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No stored pictures found. Figures drawn with vector "
                    "commands need a renderer.",
                )
            )
        incomplete = any(
            f.code not in PikepdfImageExtractor._INFORMATIONAL for f in findings
        )
        return ImageExtractResult(
            extractor=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            images=tuple(images),
            findings=tuple(findings),
        )

    @staticmethod
    def _pictures(page: Any) -> list[Any]:
        """Distinct image objects the page lists, nested forms included."""
        pictures: list[Any] = []
        seen: set[tuple[int, int]] = set()
        for picture in page.get_images().values():
            key = picture.objgen
            if key != (0, 0) and key in seen:
                continue
            seen.add(key)
            pictures.append(picture)
        return pictures

    @staticmethod
    def _has_inline_images(pikepdf: Any, page: Any) -> bool:
        """Whether the page or a form it lists draws an inline image."""
        seen: set[tuple[int, int]] = set()
        pending: list[tuple[Any, Any]] = [(page, page.resources)]
        while pending:
            content, resources = pending.pop()
            # Filtered on "EI", pikepdf returns each inline image as a plain
            # instruction with that operator, not as a ContentStreamInlineImage.
            if any(
                str(instruction.operator) == "EI"
                for instruction in pikepdf.parse_content_stream(content, "EI")
            ):
                return True
            xobjects = (
                resources.get("/XObject")
                if isinstance(resources, pikepdf.Dictionary)
                else None
            )
            if xobjects is None or not isinstance(xobjects, pikepdf.Dictionary):
                continue
            for _, xobject in xobjects.items():
                if (
                    isinstance(xobject, pikepdf.Stream)
                    and xobject.get("/Subtype") == pikepdf.Name.Form
                    and xobject.objgen not in seen
                ):
                    seen.add(xobject.objgen)
                    pending.append((xobject, xobject.get("/Resources", resources)))
        return False

    @staticmethod
    def _extract_one(
        pikepdf: Any,
        picture: Any,
        index: int,
        errors: tuple[type[BaseException], ...],
    ) -> ExtractedImage | ImageExtractFinding:
        unsupported = (
            pikepdf.UnsupportedImageTypeError,
            pikepdf.HifiPrintImageNotTranscodableError,
            pikepdf.DependencyError,
            UnidentifiedImageError,
        )
        try:
            image = pikepdf.PdfImage(picture)
            # pikepdf decodes pixels without Pillow's decompression-bomb check,
            # so apply Pillow's own ceiling before decoding.
            if too_many_pixels(image.width, image.height):
                return too_large(index)
            buffer = BytesIO()
            image.extract_to(stream=buffer)
            data, media_type, width, height = image_file(buffer.getvalue())
            original = data == picture.read_raw_bytes()
        except Image.DecompressionBombError:
            return too_large(index)
        except unsupported:
            return finding(
                ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
                "One picture uses an encoding that cannot be turned into an "
                "image file.",
                index,
            )
        except errors:
            return finding(
                ImageFindingCode.IMAGE_UNREADABLE,
                "One picture on this page could not be extracted.",
                index,
            )
        return ExtractedImage(
            data=data,
            media_type=media_type,
            width=width,
            height=height,
            page_index=index,
            method="original" if original else "reencoded",
        )

    def _failure(self, code: ImageFindingCode, message: str) -> ImageExtractResult:
        return failure(self.name, code, message)
