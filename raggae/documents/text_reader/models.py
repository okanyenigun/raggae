"""Text blocks and reading findings shared by document text readers."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator

type TextReadOutcome = Literal["success", "partial", "failed", "not_applicable"]
type TextReadMethod = Literal["native", "ocr", "vision", "mixed", "unknown"]


class TextContentFormat(StrEnum):
    """How the text inside a block is written (not the source file's type)."""

    PLAIN_TEXT = "plain_text"  # Ordinary text; any markup was removed or never existed.
    MARKDOWN = "markdown"  # Markdown source, syntax included.
    JSON = "json"  # A serialized JSON value.
    XML = "xml"  # An XML element.


class FindingCode(StrEnum):
    """Why a read was incomplete, empty, or limited; the same meaning in every reader."""

    # Source and decoding
    SOURCE_UNREADABLE = (
        "source_unreadable"  # Missing, unreadable, or not a regular file.
    )
    DECODE_FAILED = "decode_failed"  # Bytes could not be decoded or the file opened.
    PARSE_FAILED = "parse_failed"  # Decoded, but the content's syntax is invalid.
    NOT_APPLICABLE = "not_applicable"  # Not a format this reader handles.
    PASSWORD_REQUIRED = "password_required"  # Encrypted; no password supplied.
    INVALID_PASSWORD = "invalid_password"  # Encrypted; the supplied password is wrong.
    NO_PAGES = "no_pages"  # The document has no usable pages.
    EMPTY = "empty"  # Read successfully, but no text in this scope.

    # Selections and layout regions
    SELECTION_UNSUPPORTED = (
        "selection_unsupported"  # Reader or region cannot be read by region.
    )
    NOTHING_SELECTED = (
        "nothing_selected"  # An empty selection: no region was asked for.
    )
    SELECTION_OVERLAP = (
        "selection_overlap"  # Selected regions share content; neither read.
    )
    REGION_UNRESOLVED = (
        "region_unresolved"  # Region's location not found in the source.
    )
    LAYOUT_MISMATCH = "layout_mismatch"  # Layout dimensions do not match the source.
    PAGE_MISSING = "page_missing"  # Requested page or frame does not exist.

    # Pages and conversion
    PAGE_UNREADABLE = "page_unreadable"  # One page or frame could not be read.
    PAGE_INCOMPLETE = (
        "page_incomplete"  # A model did not finish a page; its text left out.
    )
    OCR_FAILED = "ocr_failed"  # The OCR service failed on a page or region.
    CONVERSION_PARTIAL = (
        "conversion_partial"  # Backend reported errors on part of the document.
    )
    CROP_NOT_APPLIED = (
        "crop_not_applied"  # Text outside the visible page area may be included.
    )
    PICTURES_UNREAD = "pictures_unread"  # Pictures on the page were not transcribed.
    BACKEND_COVERAGE = "backend_coverage"  # The backend may not cover all content.

    # Structured content
    UNSUPPORTED_CONTENT = (
        "unsupported_content"  # Content of an unsupported kind was skipped.
    )
    STRUCTURE_UNSUPPORTED = (
        "structure_unsupported"  # Document structure this reader cannot map.
    )
    HTML_OMITTED = "html_omitted"  # Raw HTML inside Markdown was left out.
    HIDDEN_SKIPPED = "hidden_skipped"  # Hidden elements were not read.
    HIDDEN_CONTENT = "hidden_content"  # Hidden content was read and is included.
    HIDDEN_SLIDE = "hidden_slide"  # A hidden slide was read.
    NOTES_UNREAD = "notes_unread"  # Speaker notes were not read.
    INHERITED_TEXT_UNREAD = (
        "inherited_text_unread"  # Layout/master placeholder text not read.
    )
    REVISIONS_PRESENT = (
        "revisions_present"  # Tracked changes exist; accepted text was read.
    )
    OTHER_PARTS_UNREAD = (
        "other_parts_unread"  # Headers, footers, notes, or comments not read.
    )
    TEXT_BOXES_UNREAD = "text_boxes_unread"  # Text boxes were not read.
    FORMULA_RESULT_MISSING = "formula_result_missing"  # A formula has no saved result.

    # Tables, records, and queries
    HEADER_BLANK = "header_blank"  # A header cell is blank.
    HEADER_RENAMED = "header_renamed"  # Duplicate headers were renamed.
    HEADER_INVALID = "header_invalid"  # A header name is not a valid XML tag name.
    INVALID_CHARACTER = (
        "invalid_character"  # A value holds a character XML cannot represent.
    )
    ROW_LENGTH_MISMATCH = (
        "row_length_mismatch"  # A row's field count differs from the header.
    )
    FIELD_TOO_LARGE = "field_too_large"  # A field exceeds the parser's size limit.
    FIELDS_DROPPED = "fields_dropped"  # The parser dropped fields from a row.
    DUPLICATE_KEY = "duplicate_key"  # A JSON object repeats a key.
    POINTER_UNRESOLVED = "pointer_unresolved"  # A JSON Pointer matched nothing.
    QUERY_FAILED = "query_failed"  # A jq query failed.


class TextBlock(BaseModel):
    """One piece of text with its source page and optional layout-region ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(
        description="The extracted content itself; not a log or a path to an output file.",
        examples=["Payment is due within 30 days."],
    )
    page_index: int = Field(
        default=0,
        ge=0,
        strict=True,
        description="Zero-based source page index. The first PDF page is 0; non-paginated documents use 0 for every block. Geometry belongs to layout analysis.",
        examples=[0, 1],
    )
    content_format: TextContentFormat = Field(
        default=TextContentFormat.PLAIN_TEXT,
        description="Representation of text: plain text, Markdown, or serialized JSON content.",
        examples=["plain_text", "markdown", "json"],
    )
    region_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None,
        description="Input layout region that produced this block, or None for standalone whole-document reading.",
        examples=[None, "page-2-r3"],
    )
    method: TextReadMethod = Field(
        default="native",
        description="How the content was obtained. Unknown means the backend did not establish its extraction method.",
        examples=["native", "ocr", "vision", "mixed", "unknown"],
    )


class TextReadFinding(BaseModel):
    """A safe explanation of an empty read, unsupported selection, or failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: FindingCode = Field(
        description="Stable machine-readable reason from FindingCode; the reader is named on the result.",
        examples=["decode_failed", "selection_unsupported"],
    )
    message: str = Field(
        min_length=1,
        description="Short explanation without raw backend errors or source excerpts.",
        examples=["Source could not be decoded using the configured encoding."],
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
        description="Affected zero-based page index, or None when no specific page is known. Non-paginated documents use 0 when their content was read.",
        examples=[None, 0, 1],
    )


class TextReadResult(BaseModel):
    """One reader's extracted blocks and coverage findings; never a changed file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(
        min_length=1,
        description="Name of the worker that performed this read.",
        examples=["text_reader_plain"],
    )
    outcome: TextReadOutcome = Field(
        description="Success means the requested scope completed; partial means some work is missing; failed means none completed; not_applicable means an unsupported format.",
        examples=["success", "partial", "failed", "not_applicable"],
    )
    blocks: tuple[TextBlock, ...] = Field(
        default=(),
        description="Text blocks with page indexes in extraction sequence, not a guarantee of semantic reading order. Empty input may have no blocks.",
    )
    findings: tuple[TextReadFinding, ...] = Field(
        default=(),
        description="Explanations of empty reads, unsupported selections, failures, and limits.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome in ("failed", "not_applicable") and self.blocks:
            raise ValueError("failed/not-applicable results cannot contain text blocks")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
