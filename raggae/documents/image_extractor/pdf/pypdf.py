"""Recover stored PDF pictures page by page with pypdf; whole pages only."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
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
    finish,
    image_file,
    prepare_source,
    read_source,
    too_large,
    too_many_pixels,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact


class PypdfImageExtractor:
    """Return the pictures pypdf lists for each page, including inline images.

    For each page, pypdf lists the image objects in its resources, those inside
    forms, and the inline images written into its drawing commands, in pypdf's
    order. A picture listed under two names on one page comes back once; one on
    ten pages comes back ten times; one listed but never drawn still comes back.

    A stored JPEG comes back byte for byte (``original``). Other pictures are
    decoded by pypdf and saved as new files (``reencoded``): PNG, or TIFF for
    CMYK and spot colours. pypdf applies soft masks, so masked pictures come
    back as PNG with transparency. JPEG 2000 is saved as PNG. A picture pypdf
    cannot decode becomes a finding, and the other pictures are kept. Pictures
    above Pillow's decompression-bomb ceiling are skipped before decoding when
    their size is declared in their dictionary (inline images are not checked
    in advance).

    Layout-region requests are unsupported: pypdf has no reliable drawing
    positions. ``PdfiumImageExtractor`` works with layouts. Passwords come from
    ``Artifact``.
    """

    _INFORMATIONAL = frozenset({ImageFindingCode.EMPTY})

    @property
    def name(self) -> str:
        return "image_extractor_pypdf"

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
                "PypdfImageExtractor returns stored pictures by page and cannot work "
                "from layout regions; use PdfiumImageExtractor for those."
            ),
        )
        if isinstance(prepared, ImageExtractResult):
            return prepared
        data = read_source(prepared, self.name)
        if isinstance(data, ImageExtractResult):
            return data
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

        from pypdf import PdfReader
        from pypdf.errors import FileNotDecryptedError, PyPdfError

        errors: tuple[type[BaseException], ...] = (
            PyPdfError,
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
            document = PdfReader(BytesIO(data))
            if document.is_encrypted and password and not document.decrypt(password):
                return failure(
                    self.name,
                    ImageFindingCode.INVALID_PASSWORD,
                    "PDF could not be opened with the supplied password.",
                )
            pages = list(document.pages)
        except FileNotDecryptedError:
            return self._password_required()
        except errors:
            return failure(
                self.name, ImageFindingCode.DECODE_FAILED, "PDF could not be opened."
            )
        if not pages:
            return failure(
                self.name, ImageFindingCode.NO_PAGES, "PDF contains no usable pages."
            )

        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        completed = found = 0
        for index, page in enumerate(pages):
            try:
                listing = page.images
                keys = list(listing.keys())
            except FileNotDecryptedError:
                return self._password_required()
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
            found += len(keys)
            seen: set[tuple[int, int]] = set()
            for position, key in enumerate(keys):
                stored = PypdfImageExtractor._xobject(page, key)
                identity = PypdfImageExtractor._identity(stored)
                if identity is not None and identity in seen:
                    continue
                if identity is not None:
                    seen.add(identity)
                if stored is not None and too_many_pixels(
                    int(stored.get("/Width", 0)), int(stored.get("/Height", 0))
                ):
                    findings.append(too_large(index))
                    continue
                try:
                    picture = listing[position]
                except FileNotDecryptedError:
                    return self._password_required()
                except NotImplementedError:
                    findings.append(PypdfImageExtractor._unsupported(index))
                    continue
                except errors:
                    findings.append(PypdfImageExtractor._unreadable(index))
                    continue
                extracted = PypdfImageExtractor._picture(picture, stored, index)
                if isinstance(extracted, ExtractedImage):
                    images.append(extracted)
                else:
                    findings.append(extracted)
        if completed and not found:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No pictures found. Figures drawn with vector commands "
                    "need PdfiumImageExtractor and a layout region.",
                )
            )
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    def _password_required(self) -> ImageExtractResult:
        return failure(
            self.name,
            ImageFindingCode.PASSWORD_REQUIRED,
            "PDF could not be opened with the supplied password.",
        )

    @staticmethod
    def _xobject(page: Any, key: Any) -> Any:
        """The image object a listing key names (through forms), or None for an inline
        image or a key that cannot be followed."""
        path = key if isinstance(key, list) else [key]
        try:
            resources = page.get("/Resources")
            resources = None if resources is None else resources.get_object()
            stored = None
            for name in path:
                if (
                    not isinstance(name, str)
                    or name.startswith("~")
                    or resources is None
                ):
                    return None
                stored = resources["/XObject"].get_object()[name].get_object()
                inner = stored.get("/Resources")
                resources = inner.get_object() if inner is not None else resources
            return stored
        except (KeyError, AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _identity(stored: Any) -> tuple[int, int] | None:
        reference = getattr(stored, "indirect_reference", None)
        return None if reference is None else (reference.idnum, reference.generation)

    @staticmethod
    def _picture(
        picture: Any, stored: Any, index: int
    ) -> ExtractedImage | ImageExtractFinding:
        try:
            data, media_type, width, height = image_file(bytes(picture.data))
            original = stored is not None and data == bytes(stored.get_data())
        except Image.DecompressionBombError:
            return too_large(index)
        except UnidentifiedImageError:
            return PypdfImageExtractor._unsupported(index)
        except Exception:  # noqa: BLE001 - pypdf's stored data may fail in many ways
            return PypdfImageExtractor._unreadable(index)
        return ExtractedImage(
            data=data,
            media_type=media_type,
            width=width,
            height=height,
            page_index=index,
            method="original" if original else "reencoded",
        )

    @staticmethod
    def _unsupported(index: int) -> ImageExtractFinding:
        return finding(
            ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
            "One picture uses an encoding that cannot be turned into an image file.",
            index,
        )

    @staticmethod
    def _unreadable(index: int) -> ImageExtractFinding:
        return finding(
            ImageFindingCode.IMAGE_UNREADABLE,
            "One picture on this page could not be extracted.",
            index,
        )
