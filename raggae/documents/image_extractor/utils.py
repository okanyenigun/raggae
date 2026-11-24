"""Helpers shared by the image extractors.

Sources and findings, region selection, image-file checks, Office package and
region-ownership rules, and data-URL pictures for Markdown and HTML.
"""

import re
from base64 import b64decode
from binascii import Error as Base64Error
from collections import Counter
from collections.abc import Hashable, Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit
from zipfile import BadZipFile, ZipFile
from PIL import Image, UnidentifiedImageError
from .models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
    ImageMediaType,
)
from ..layout_analysis.models import LayoutAnalysisResult, LayoutRegion
from ..normalization.artifacts import Artifact
from ..shared.sources import as_artifact, select_regions

# Pillow format names of the image files returned as they are. MPO is Pillow's
# name for a JPEG that carries a multi-picture marker; it is still a JPEG file.
MEDIA_TYPES: dict[str, ImageMediaType] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def failure(
    extractor: str,
    code: ImageFindingCode,
    message: str,
    *,
    page_index: int | None = None,
) -> ImageExtractResult:
    return ImageExtractResult(
        extractor=extractor,
        outcome="failed",
        findings=(
            ImageExtractFinding(code=code, message=message, page_index=page_index),
        ),
    )


def prepare_source(
    source: Path | str | bytes | Artifact,
    *,
    extractor: str,
    layout: LayoutAnalysisResult | None,
    region_ids: Sequence[str] | None,
    unsupported_message: str,
) -> Artifact | ImageExtractResult:
    """Resolve input and reject layout selection before source IO.

    For extractors that cannot work from layout regions: any layout is refused,
    one finding per selected region, or one finding without a region when the
    selection is empty.
    """
    source = as_artifact(source)
    selected = select_regions(layout, region_ids)
    if selected is None:
        return source
    return ImageExtractResult(
        extractor=extractor,
        outcome="failed",
        findings=tuple(
            ImageExtractFinding(
                code=ImageFindingCode.SELECTION_UNSUPPORTED,
                message=unsupported_message,
                region_id=region.id,
            )
            for region in selected
        )
        or (
            ImageExtractFinding(
                code=ImageFindingCode.SELECTION_UNSUPPORTED,
                message=unsupported_message,
            ),
        ),
    )


def select_image_regions(
    layout: LayoutAnalysisResult | None, region_ids: Sequence[str] | None
) -> tuple[LayoutRegion, ...] | None:
    """None without a layout; the image regions when no IDs are given; else exactly the IDs.

    Explicit IDs are honoured whatever their kind, so a caller can ask for a
    table or chart region as a picture.
    """
    selected = select_regions(layout, region_ids)
    if selected is not None and region_ids is None:
        selected = tuple(region for region in selected if region.kind == "image")
    return selected


def nothing_selected(extractor: str, *, explicit: bool) -> ImageExtractResult:
    """A valid request for zero regions: success, with a finding saying why.

    ``explicit`` is an empty ``region_ids``; otherwise the layout had no image
    regions to select by default.
    """
    message = (
        "No regions were selected, so nothing was extracted."
        if explicit
        else "The layout has no image regions, so nothing was extracted."
    )
    return ImageExtractResult(
        extractor=extractor,
        outcome="success",
        findings=(
            ImageExtractFinding(
                code=ImageFindingCode.NOTHING_SELECTED, message=message
            ),
        ),
    )


def too_many_pixels(width: float, height: float) -> bool:
    """Above Pillow's decompression-bomb ceiling (twice Image.MAX_IMAGE_PIXELS)."""
    limit = Image.MAX_IMAGE_PIXELS
    return limit is not None and width * height > 2 * limit


def image_file(data: bytes) -> tuple[bytes, ImageMediaType, int, int]:
    """Confirm an image file; save other formats (JPEG 2000) as a new file.

    Returns the bytes, their media type, and the pixel size.
    """
    with Image.open(BytesIO(data)) as picture:
        media_type = MEDIA_TYPES.get(picture.format or "")
        if media_type is None:
            picture.load()
            data, media_type = encode(picture)
        return data, media_type, picture.width, picture.height


# Pixel modes a PNG can hold; anything else (CMYK, floating point) becomes a TIFF.
_PNG_MODES = frozenset({"1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"})


def encode(image: Image.Image) -> tuple[bytes, ImageMediaType]:
    """Save pixels as a new file: PNG when the mode allows it, otherwise TIFF."""
    target = "PNG" if image.mode in _PNG_MODES else "TIFF"
    with BytesIO() as output:
        image.save(output, target)
        return output.getvalue(), MEDIA_TYPES[target]


def read_source(
    source: Artifact, extractor: str, *, allow_empty: bool = False
) -> bytes | ImageExtractResult:
    """The source's bytes, or a failed result for a missing or irregular file.

    An empty source fails too, unless ``allow_empty`` (an empty text file is a
    valid, empty document).
    """
    try:
        if source.path is not None and not S_ISREG(source.path.stat().st_mode):
            return failure(
                extractor,
                ImageFindingCode.SOURCE_UNREADABLE,
                "Source must be a regular file.",
            )
        data = source.read_bytes()
    except OSError:
        return failure(
            extractor, ImageFindingCode.SOURCE_UNREADABLE, "Source could not be read."
        )
    if not data and not allow_empty:
        return failure(extractor, ImageFindingCode.DECODE_FAILED, "Source is empty.")
    return data


# Picture types that cannot become pixels here (vector or legacy formats).
_VECTOR_TYPES = frozenset(
    {
        "image/x-emf",
        "image/emf",
        "image/x-wmf",
        "image/wmf",
        "image/svg+xml",
        "image/x-pict",
    }
)


def finding(
    code: ImageFindingCode,
    message: str,
    page_index: int | None = None,
    region_id: str | None = None,
) -> ImageExtractFinding:
    return ImageExtractFinding(
        code=code, message=message, page_index=page_index, region_id=region_id
    )


def too_large(
    page_index: int | None = None, region_id: str | None = None
) -> ImageExtractFinding:
    """A picture above Pillow's decompression-bomb ceiling, skipped before decoding."""
    return finding(
        ImageFindingCode.IMAGE_TOO_LARGE,
        "One picture is above Pillow's decompression-bomb limit and was skipped.",
        page_index,
        region_id,
    )


def stored_picture(
    data: bytes,
    *,
    page_index: int,
    region_id: str | None = None,
    alt_text: str | None = None,
    content_type: str | None = None,
) -> ExtractedImage | ImageExtractFinding:
    """A picture file stored in a document: kept as it is, or saved as a new file.

    PNG, JPEG, TIFF, GIF, and WebP come back byte for byte (``original``);
    other raster formats (BMP, JPEG 2000) become new files (``reencoded``);
    vector and legacy formats (EMF, WMF, SVG) become a finding.
    """

    unsupported = (
        ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
        "One picture uses a format that cannot be turned into an image file.",
        page_index,
        region_id,
    )
    if content_type in _VECTOR_TYPES:
        return finding(*unsupported)
    try:
        with Image.open(BytesIO(data)) as probe:
            if probe.format == "WMF":  # Pillow's name for both WMF and EMF
                return finding(*unsupported)
            if too_many_pixels(*probe.size):
                return too_large(page_index, region_id)
        output, media_type, width, height = image_file(data)
    except Image.DecompressionBombError:
        return too_large(page_index, region_id)
    except UnidentifiedImageError:
        return finding(*unsupported)
    except (OSError, ValueError, SyntaxError, EOFError):
        return finding(
            ImageFindingCode.IMAGE_UNREADABLE,
            "One picture could not be extracted.",
            page_index,
            region_id,
        )
    return ExtractedImage(
        data=output,
        media_type=media_type,
        width=width,
        height=height,
        page_index=page_index,
        region_id=region_id,
        method="original" if output == data else "reencoded",
        alt_text=alt_text,
    )


def finish(
    extractor: str,
    images: list[ExtractedImage],
    findings: list[ImageExtractFinding],
    completed: int,
    informational: frozenset[ImageFindingCode],
) -> ImageExtractResult:
    """The result: partial when a non-informational finding exists, failed with no work."""
    incomplete = any(f.code not in informational for f in findings)
    return ImageExtractResult(
        extractor=extractor,
        outcome=("partial" if incomplete else "success") if completed else "failed",
        images=tuple(images),
        findings=tuple(findings),
    )


def png(image: Image.Image) -> bytes:
    with BytesIO() as output:
        image.save(output, "PNG")
        return output.getvalue()


# Office packages and region ownership ----------------------------------------

# XML names that python-docx's and python-pptx's ``qn`` do not know.
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
VML_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
VML_SHAPE = "{urn:schemas-microsoft-com:vml}shape"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
R_LINK = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link"


def package_names(data: bytes, extractor: str) -> list[str] | ImageExtractResult:
    """The part names of an Office package, or a failed result when it is not one."""
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
    except BadZipFile:
        return failure(
            extractor,
            ImageFindingCode.DECODE_FAILED,
            "Source is not a readable Office document package.",
        )
    if len(set(names)) != len(names) or "[Content_Types].xml" not in names:
        return failure(
            extractor,
            ImageFindingCode.DECODE_FAILED,
            "Source is not a readable Office document package.",
        )
    return names


def owners[K: Hashable](
    claims: Sequence[tuple[Sequence[int], Sequence[K]]],
) -> tuple[list[list[K]], set[int]]:
    """Which region owns each picture, by the text readers' rules.

    ``claims`` holds, per region, its location path and the pictures inside it.
    The innermost (longest-path) region owns a picture; regions with the same
    path locate the same element and are both refused. Returns the pictures
    each region owns, in its own order, and the refused regions' positions.
    """
    paths = Counter(tuple(path) for path, _ in claims)
    refused = {n for n, (path, _) in enumerate(claims) if paths[tuple(path)] > 1}
    best: dict[K, tuple[int, int]] = {}
    for number, (path, pictures) in enumerate(claims):
        if number in refused:
            continue
        for picture in pictures:
            if picture not in best or len(path) > best[picture][0]:
                best[picture] = (len(path), number)
    owned = [
        [p for p in pictures if best.get(p, (0, -1))[1] == number]
        for number, (_, pictures) in enumerate(claims)
    ]
    return owned, refused


def region_finding(
    code: ImageFindingCode, message: str, region_id: str, page_index: int | None
) -> ImageExtractFinding:
    return ImageExtractFinding(
        code=code, message=message, region_id=region_id, page_index=page_index
    )


def overlap(region_id: str, page_index: int | None) -> ImageExtractFinding:
    return region_finding(
        ImageFindingCode.SELECTION_OVERLAP,
        "Selected regions locate the same element; neither was extracted.",
        region_id,
        page_index,
    )


def empty_region(region_id: str, page_index: int | None) -> ImageExtractFinding:
    return region_finding(
        ImageFindingCode.EMPTY,
        "No picture in this region.",
        region_id,
        page_index,
    )


def unresolved(region_id: str, page_index: int | None) -> ImageExtractFinding:
    return region_finding(
        ImageFindingCode.REGION_UNRESOLVED,
        "The region's location was not found in this document.",
        region_id,
        page_index,
    )


def unsupported_selection(region_id: str, message: str) -> ImageExtractFinding:
    return region_finding(
        ImageFindingCode.SELECTION_UNSUPPORTED, message, region_id, None
    )


def walk(node: Any, skip: frozenset[Any], stop: frozenset[Any]) -> Any:
    """Elements under ``node`` (itself included) in document order.

    Subtrees whose tag is in ``skip`` are left out; elements whose tag is in
    ``stop`` are yielded without their children.
    """
    pending = [node]
    while pending:
        item = pending.pop()
        if item.tag in skip:
            continue
        yield item
        if item.tag not in stop:
            pending.extend(reversed(list(item)))


# Markdown and HTML pictures ----------------------------------------------------

# Raw HTML that draws a picture, inside Markdown.
HTML_PICTURE = re.compile(r"<\s*(img|picture|svg)\b", re.IGNORECASE)
_LONGEST_TARGET = 200


def picture_at(
    target: str, *, alt_text: str | None
) -> ExtractedImage | ImageExtractFinding:
    """The picture a target holds: a data URL decoded, or a finding.

    A data URL is decoded and kept as a stored file. Any other target names a
    picture in another file, which is never opened or fetched; the finding
    names it, without user names, passwords, queries, or fragments.
    """
    if not target.lower().startswith("data:"):
        message = (
            f"A picture refers to another file, which was not opened or fetched: "
            f"{describe(target)}"
            if target.strip()
            else "A picture has no target."
        )
        return finding(ImageFindingCode.REFERENCE_UNRESOLVED, message, page_index=0)
    header, separator, payload = target[5:].partition(",")
    parameters = header.split(";")
    media_type = parameters[0].strip().lower() or "text/plain"
    if not separator:
        return finding(
            ImageFindingCode.IMAGE_UNREADABLE,
            "A picture written in the document could not be decoded.",
            page_index=0,
        )
    if not media_type.startswith("image/"):
        return finding(
            ImageFindingCode.UNSUPPORTED_IMAGE_TYPE,
            "A data URL in a picture holds something other than an image.",
            page_index=0,
        )
    try:
        if parameters[-1].strip().lower() == "base64":
            data = b64decode(re.sub(r"\s+", "", payload), validate=True)
        else:
            data = unquote_to_bytes(payload)
    except (Base64Error, ValueError):
        return finding(
            ImageFindingCode.IMAGE_UNREADABLE,
            "A picture written in the document could not be decoded.",
            page_index=0,
        )
    if not data:
        return finding(
            ImageFindingCode.IMAGE_UNREADABLE,
            "A picture written in the document is empty.",
            page_index=0,
        )
    return stored_picture(
        data, page_index=0, alt_text=alt_text, content_type=media_type
    )


def describe(target: str) -> str:
    """A target as written, without credentials, queries, or fragments, shortened."""
    parts = urlsplit(target)
    if parts.scheme and parts.netloc:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        target = f"{parts.scheme}://{host}{parts.path}"
    elif "?" in target or "#" in target:
        target = re.split(r"[?#]", target, maxsplit=1)[0]
    if len(target) > _LONGEST_TARGET:
        target = target[: _LONGEST_TARGET - 1] + "…"
    return target
