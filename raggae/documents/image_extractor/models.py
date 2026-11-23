"""Extracted pictures and extraction findings shared by document image extractors."""

from base64 import b64encode
from enum import StrEnum
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator

type ImageExtractOutcome = Literal["success", "partial", "failed", "not_applicable"]
type ImageExtractMethod = Literal["original", "reencoded", "rendered"]
type ImageMediaType = Literal[
    "image/png", "image/jpeg", "image/tiff", "image/gif", "image/webp"
]


class ImageFindingCode(StrEnum):
    """Why an extraction was incomplete, empty, or limited; the same meaning in every extractor."""

    # Source and decoding (same meaning as the text-reader codes of the same name)
    SOURCE_UNREADABLE = (
        "source_unreadable"  # Missing, unreadable, or not a regular file.
    )
    DECODE_FAILED = "decode_failed"  # Bytes could not be decoded or the file opened.
    PARSE_FAILED = "parse_failed"  # Decoded, but the content's syntax is invalid.
    NOT_APPLICABLE = "not_applicable"  # Not a format this extractor handles.
    PASSWORD_REQUIRED = "password_required"  # Encrypted; no password supplied.
    INVALID_PASSWORD = "invalid_password"  # Encrypted; the supplied password is wrong.
    NO_PAGES = "no_pages"  # The document has no usable pages.
    EMPTY = "empty"  # Extracted successfully, but no pictures in this scope.

    # Selections and layout regions
    SELECTION_UNSUPPORTED = (
        "selection_unsupported"  # Extractor or region cannot be read by region.
    )
    NOTHING_SELECTED = (
        "nothing_selected"  # An empty selection, or a layout with no image regions.
    )
    SELECTION_OVERLAP = (
        "selection_overlap"  # Selected regions claim the same picture; neither read.
    )
    LAYOUT_MISMATCH = "layout_mismatch"  # Layout dimensions do not match the source.
    PAGE_MISSING = "page_missing"  # Requested page does not exist.
    REGION_UNRESOLVED = (
        "region_unresolved"  # Region's location not found in the source.
    )

    # Pages and pictures
    PAGE_UNREADABLE = "page_unreadable"  # One page's pictures could not be listed.
    IMAGE_UNREADABLE = "image_unreadable"  # One picture could not be extracted.
    UNSUPPORTED_IMAGE_TYPE = (
        "unsupported_image_type"  # A picture's encoding cannot become an image file.
    )
    IMAGE_TOO_LARGE = (
        "image_too_large"  # Above Pillow's decompression-bomb ceiling; skipped.
    )
    INLINE_IMAGES_SKIPPED = (
        "inline_images_skipped"  # Pictures written into drawing commands not extracted.
    )
    REFERENCE_UNRESOLVED = (
        "reference_unresolved"  # A picture is stored outside the file; not fetched.
    )
    CROP_NOT_APPLIED = (
        "crop_not_applied"  # Shown cropped; the whole stored picture was returned.
    )

    # Document structure
    OTHER_PARTS_UNREAD = "other_parts_unread"  # Pictures in headers, footers, masters, or notes not read.
    UNSUPPORTED_CONTENT = (
        "unsupported_content"  # Charts, diagrams, objects, or fills: not pictures.
    )
    HIDDEN_SLIDE = "hidden_slide"  # A hidden slide was read.
    HIDDEN_SKIPPED = "hidden_skipped"  # Hidden elements were not read.
    STRUCTURE_UNSUPPORTED = (
        "structure_unsupported"  # Document structure this extractor cannot map.
    )
    BACKEND_COVERAGE = (
        "backend_coverage"  # The backend dropped part of a drawing while loading.
    )


class ExtractedImage(BaseModel):
    """One picture as a complete image file, with its source page and optional layout-region ID."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    data: bytes = Field(
        min_length=1,
        repr=False,
        description="A complete image file in memory that decodes on its own; never a path or an image object. JSON carries it as base64.",
    )
    media_type: ImageMediaType = Field(
        description="Encoding of data.",
        examples=["image/png", "image/jpeg"],
    )
    width: int = Field(
        ge=1,
        strict=True,
        description="Pixel width of data.",
        examples=[1200],
    )
    height: int = Field(
        ge=1,
        strict=True,
        description="Pixel height of data.",
        examples=[800],
    )
    page_index: int = Field(
        default=0,
        ge=0,
        strict=True,
        description="Zero-based source page, slide, or frame. The first PDF page is 0; documents without pages use 0 for every image. Geometry belongs to layout analysis.",
        examples=[0, 3],
    )
    region_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="Input layout region that produced this image, or None for standalone whole-document extraction.",
        examples=[None, "slide-4-r2"],
    )
    method: ImageExtractMethod = Field(
        description="original: the stored image file, byte for byte; reencoded: stored pixels saved as a new file; rendered: a visible area drawn to pixels.",
        examples=["original", "reencoded", "rendered"],
    )
    alt_text: str | None = Field(
        default=None,
        description="Description written by the document's author, exactly as stored; None when the source has none. Never a generated caption.",
        examples=[None, "Pump assembly"],
    )

    def to_base64(self) -> str:
        """The image file in data as a standard base64 string (with + and /).

        This is the form image APIs and data URLs expect. JSON from
        model_dump_json() carries data as URL-safe base64 (with - and _) instead.
        """
        return b64encode(self.data).decode("ascii")


class ImageExtractFinding(BaseModel):
    """A safe explanation of an empty extraction, unsupported selection, or failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ImageFindingCode = Field(
        description="Stable machine-readable reason from ImageFindingCode; the extractor is named on the result.",
        examples=["unsupported_image_type", "selection_unsupported"],
    )
    message: str = Field(
        min_length=1,
        description="Short explanation without raw backend errors or source excerpts.",
        examples=[
            "One picture uses an encoding that cannot be turned into an image file."
        ],
    )
    region_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="Affected input layout region, when this finding concerns a selection.",
        examples=[None, "page-2-r3"],
    )
    page_index: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Affected zero-based page index, or None when no specific page is known.",
        examples=[None, 0, 1],
    )


class ImageExtractResult(BaseModel):
    """One extractor's pictures and coverage findings; never a changed file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extractor: str = Field(
        min_length=1,
        description="Name of the worker that performed this extraction.",
        examples=["image_extractor_pikepdf"],
    )
    outcome: ImageExtractOutcome = Field(
        description="Success means the requested scope completed; partial means some work is missing; failed means none completed; not_applicable means an unsupported format.",
        examples=["success", "partial", "failed", "not_applicable"],
    )
    images: tuple[ExtractedImage, ...] = Field(
        default=(),
        description="Pictures with page indexes in extraction sequence, not a guarantee of reading order. A document without pictures has none.",
    )
    findings: tuple[ImageExtractFinding, ...] = Field(
        default=(),
        description="Explanations of empty extractions, unsupported selections, failures, and limits.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome in ("failed", "not_applicable") and self.images:
            raise ValueError("failed/not-applicable results cannot contain images")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
