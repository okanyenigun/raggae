"""Inputs and located regions for layout analysis, without extracted content."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Annotated, Literal, Self
from .utils import cell_bounds


type BoundingBox = tuple[float, float, float, float]
type RegionKind = Literal["text", "heading", "caption", "table", "image", "unknown"]
type LayoutOutcome = Literal["success", "partial", "failed", "not_applicable"]


class PdfPageLocation(BaseModel):
    """One-based page number in the exact source copy that was analyzed."""

    model_config = ConfigDict(frozen=True)

    page_number: int = Field(
        ge=1,
        strict=True,
        description="One-based page number in the exact source PDF; 1 means its first page.",
        examples=[1, 3],
    )


class ImageFrameLocation(BaseModel):
    """One-based source frame in Pillow's decoded pixel coordinates.

    Pillow applies TIFF orientation while decoding, unlike JPEG/PNG EXIF.
    Resolve the same frame with Pillow before cropping these pixel boxes.
    """

    model_config = ConfigDict(frozen=True)
    frame_number: int = Field(
        ge=1,
        strict=True,
        description="One-based frame number in the source image; 1 is the only frame of an ordinary PNG or JPEG. Coordinates follow Pillow decoding.",
        examples=[1, 2],
    )


class DocxBodyLocation(BaseModel):
    """The continuous main document body, not a rendered page."""

    model_config = ConfigDict(frozen=True)
    part: str = Field(
        min_length=1,
        description="ZIP entry containing the Word document body, without a leading slash. Open this part to resolve body element locations.",
        examples=["word/document.xml"],
    )
    element: Literal["body"] = Field(
        default="body",
        description="Identifies the continuous Word body, which has no rendered page boundaries.",
        examples=["body"],
    )


class DocxElementLocation(BaseModel):
    """Zero-based element-child indexes, starting at the document body.

    Resolve the package part, find its w:body, then follow child_indexes using
    list(element)[index]. The locator identifies structure without copying text.
    """

    model_config = ConfigDict(frozen=True)
    part: str = Field(
        min_length=1,
        description="Word ZIP entry containing this element, without a leading slash.",
        examples=["word/document.xml"],
    )
    child_indexes: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = Field(
        min_length=1,
        description="Zero-based XML element-child indexes starting at w:body. For [2, 0], select the third body child, then its first child.",
        examples=[[2], [2, 0]],
    )


class HtmlDocumentLocation(BaseModel):
    """The parsed document root, including HTML fragments without a body tag."""

    model_config = ConfigDict(frozen=True)
    element: Literal["document"] = Field(
        description="Identifies the parsed HTML document root, including fragments with no body tag.",
        examples=["document"],
    )


class HtmlElementLocation(BaseModel):
    """Element-only child indexes from BeautifulSoup's html.parser root."""

    model_config = ConfigDict(frozen=True)
    child_indexes: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = Field(
        min_length=1,
        description="Zero-based element-only child indexes from the BeautifulSoup html.parser root; text nodes and comments do not count.",
        examples=[[0, 1, 0]],
    )
    tag: str = Field(
        min_length=1,
        description="HTML tag name at this source location. It describes markup, not a browser-rendered rectangle.",
        examples=["p", "table", "img"],
    )


class PptxSlideLocation(BaseModel):
    model_config = ConfigDict(frozen=True)
    slide_number: int = Field(
        ge=1,
        strict=True,
        description="One-based slide position in the source presentation, including slides with no regions.",
        examples=[1, 3],
    )
    part: str = Field(
        min_length=1,
        description="Original slide ZIP entry, without a leading slash. Its filename can differ from the slide position after reordering.",
        examples=["ppt/slides/slide2.xml"],
    )


class PptxShapeLocation(BaseModel):
    """Follow zero-based indexes through slide.shapes and nested group.shapes."""

    model_config = ConfigDict(frozen=True)
    part: str = Field(
        min_length=1,
        description="Original slide ZIP entry containing this shape, without a leading slash.",
        examples=["ppt/slides/slide2.xml"],
    )
    shape_id: int = Field(
        ge=1,
        strict=True,
        description="Shape identifier stored in the slide XML. It is not the shape list index.",
        examples=[3, 12],
    )
    shape_path: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = Field(
        min_length=1,
        description="Zero-based indexes through slide.shapes and nested group.shapes. [0, 1] selects the second child of the first top-level group.",
        examples=[[0], [0, 1]],
    )


class XlsxSheetLocation(BaseModel):
    """Resolve by exact sheet name in the original workbook."""

    model_config = ConfigDict(frozen=True)
    sheet_number: int = Field(
        ge=1,
        strict=True,
        description="One-based worksheet position in the source workbook.",
        examples=[1, 2],
    )
    sheet_name: str = Field(
        min_length=1,
        description="Exact worksheet name used to reopen the same sheet; preserve spaces and capitalization.",
        examples=["Sales 2026"],
    )


class XlsxRangeLocation(BaseModel):
    model_config = ConfigDict(frozen=True)
    sheet_name: str = Field(
        min_length=1,
        description="Exact name of the worksheet containing this cell or range.",
        examples=["Sales 2026"],
    )
    cell_range: str = Field(
        description="Uppercase A1 cell or rectangular range, with ordered endpoints and no dollar signs or sheet prefix. A candidate block may include empty cells.",
        examples=["A1:D8", "C5"],
    )

    @field_validator("cell_range")
    @classmethod
    def _range(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) not in (1, 2):
            raise ValueError("expected a cell or rectangular cell range")
        left, top = cell_bounds(parts[0])
        right, bottom = cell_bounds(parts[-1])
        if left > right or top > bottom:
            raise ValueError("cell range endpoints must be ordered")
        return value


class XlsxDrawingLocation(BaseModel):
    """Index into openpyxl's supported image/chart collection on this sheet."""

    model_config = ConfigDict(frozen=True)
    sheet_name: str = Field(
        min_length=1,
        description="Exact worksheet name containing the supported image or chart.",
        examples=["Sales 2026"],
    )
    drawing_kind: Literal["image", "chart"] = Field(
        description="Selects which openpyxl collection drawing_index refers to: the sheet images or charts.",
        examples=["image", "chart"],
    )
    drawing_index: int = Field(
        ge=0,
        strict=True,
        description="Zero-based index in the selected image or chart collection after reopening the same workbook with the supported backend.",
        examples=[0, 2],
    )
    anchor_cell: str | None = Field(
        default=None,
        description="Uppercase A1 anchor cell when available. An anchor locates an attachment point, not the full drawing bounds; None means no cell anchor is available.",
        examples=["B4", None],
    )

    @field_validator("anchor_cell")
    @classmethod
    def _anchor(cls, value: str | None) -> str | None:
        if value is not None:
            cell_bounds(value)
        return value


class LayoutSurface(BaseModel):
    """A page/slide with geometry or native flow/sheet structure without it."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id_: str = Field(
        min_length=1,
        description="Surface identifier unique within this result. Regions refer to it through surface_id; it is not a permanent document identity.",
        examples=["page-1", "sheet-2"],
    )
    kind: Literal["page", "flow", "slide", "sheet", "image"] = Field(
        default="page",
        description="Coordinate-bearing page, slide, or image frame, or unrendered flow/sheet structure. This determines the required locator and geometry.",
        examples=["page", "flow", "slide", "sheet", "image"],
    )
    location: (
        PdfPageLocation
        | DocxBodyLocation
        | HtmlDocumentLocation
        | PptxSlideLocation
        | XlsxSheetLocation
        | ImageFrameLocation
    ) = Field(
        description="Source locator for this surface: PDF page, image frame, Word body, HTML document, slide, or worksheet. Must match kind.",
        examples=[{"page_number": 1}, {"sheet_number": 2, "sheet_name": "Sales 2026"}],
    )
    width: float | None = Field(
        default=None,
        gt=0,
        description="Positive surface width in unit. PDF pages use their displayed crop, slides use slide dimensions, and images use decoded pixels. None for unrendered flows/sheets.",
        examples=[612.0, 1280.0, None],
    )
    height: float | None = Field(
        default=None,
        gt=0,
        description="Positive surface height in unit. None for unrendered Word/HTML flows and worksheet surfaces.",
        examples=[792.0, 720.0, None],
    )
    unit: Literal["pt", "px"] | None = Field(
        default=None,
        description="Geometry unit: pt for PDF pages/slides, px for image frames, and None for flows/sheets. Automatically filled from kind when geometry is present.",
        examples=["pt", "px", None],
    )

    @model_validator(mode="after")
    def _surface_geometry(self) -> Self:
        if self.kind == "page":
            if (
                not isinstance(self.location, PdfPageLocation)
                or self.width is None
                or self.height is None
            ):
                raise ValueError("a PDF page requires a page locator and dimensions")
            if self.unit not in (None, "pt"):
                raise ValueError("PDF dimensions use points")
            object.__setattr__(self, "unit", "pt")
        elif self.kind == "slide":
            if (
                not isinstance(self.location, PptxSlideLocation)
                or self.width is None
                or self.height is None
            ):
                raise ValueError("a slide requires a slide locator and dimensions")
            if self.unit not in (None, "pt"):
                raise ValueError("slide dimensions use points")
            object.__setattr__(self, "unit", "pt")
        elif self.kind == "image":
            if (
                not isinstance(self.location, ImageFrameLocation)
                or self.width is None
                or self.height is None
                or self.unit not in (None, "px")
            ):
                raise ValueError(
                    "an image requires a frame locator and pixel dimensions"
                )
            object.__setattr__(self, "unit", "px")
        elif self.kind == "sheet":
            if not isinstance(self.location, XlsxSheetLocation) or any(
                value is not None for value in (self.width, self.height, self.unit)
            ):
                raise ValueError(
                    "a sheet requires a sheet locator and has no page geometry"
                )
        elif not isinstance(
            self.location, (DocxBodyLocation, HtmlDocumentLocation)
        ) or any(value is not None for value in (self.width, self.height, self.unit)):
            raise ValueError("a flow requires a body locator and has no page geometry")
        return self


class LayoutRegion(BaseModel):
    """A positioned area or a located flow element.

    Page boxes use points from the displayed crop's top-left corner. Flow
    elements instead have a structural location and no box.

    List order is not reading order. Native PDF content-stream order is not
    advertised as native reading order either.
    """

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: str = Field(
        min_length=1,
        description="Region identifier unique within this result. Ordering and relationship workers use this ID to refer to the same region.",
        examples=["page-1-r1"],
    )
    surface_id: str = Field(
        min_length=1,
        description="ID of the returned surface containing this region; must match a LayoutSurface.id_.",
        examples=["page-1"],
    )
    kind: RegionKind = Field(
        description="Detected content category. A label is a layout candidate, not extracted content or a guarantee of semantic correctness.",
        examples=["text", "heading", "table", "image", "caption", "unknown"],
    )
    bbox: (
        tuple[
            Annotated[float, Field(ge=0)],
            Annotated[float, Field(ge=0)],
            Annotated[float, Field(gt=0)],
            Annotated[float, Field(gt=0)],
        ]
        | None
    ) = Field(
        default=None,
        description="Rectangle (left, top, right, bottom) measured from the surface top-left in its unit. It must fit the surface. None when only a structural location is available.",
        examples=[[48.0, 80.0, 300.0, 140.0], None],
    )
    location: (
        DocxElementLocation
        | HtmlElementLocation
        | PptxShapeLocation
        | XlsxRangeLocation
        | XlsxDrawingLocation
        | None
    ) = Field(
        default=None,
        description="Native source location for reopening a Word/HTML element, slide shape, worksheet range, or worksheet drawing. Required when bbox is unavailable; PDF/image regions use their surface locator and bbox.",
        examples=[
            {"part": "word/document.xml", "child_indexes": [2]},
            {"sheet_name": "Sales 2026", "cell_range": "A1:D8"},
            None,
        ],
    )
    native_order: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Zero-based traversal position when the source exposes one. It does not guarantee visual or semantic reading order. None when no native order is supplied.",
        examples=[0, 3, None],
    )

    @model_validator(mode="after")
    def _ordered_box(self) -> Self:
        if self.bbox is None:
            if self.location is None:
                raise ValueError("a region requires a bbox or structural location")
        else:
            left, top, right, bottom = self.bbox
            if right <= left or bottom <= top:
                raise ValueError("bbox must have positive width and height")
        return self


class LayoutFinding(BaseModel):
    """A safe explanation of a gap; never a password or raw parser diagnostic."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(
        min_length=1,
        description="Machine-readable reason for a limitation or failure. Use this stable code rather than matching message text.",
        examples=["layout.pdf.object_limit"],
    )
    message: str = Field(
        min_length=1,
        description="Human-readable explanation of the issue; excludes passwords, source text samples, and raw backend diagnostics.",
        examples=["Page regions were not analyzed: object limit exceeded."],
    )
    surface_id: str | None = Field(
        default=None,
        description="ID of the affected surface, or None when the issue applies to the source or analysis as a whole.",
        examples=["page-1", None],
    )
    region_id: str | None = Field(
        default=None,
        description="ID of the affected region when a specific returned region can be identified; otherwise None.",
        examples=["page-1-r1", None],
    )


class LayoutAnalysisResult(BaseModel):
    """The document map and coverage findings, not a new document artifact."""

    model_config = ConfigDict(frozen=True)

    outcome: LayoutOutcome = Field(
        description="success means analysis completed; partial means a usable map has reported gaps; failed means no usable map; not_applicable means unsupported input. Success does not guarantee detection accuracy.",
        examples=["success", "partial", "failed", "not_applicable"],
    )
    surfaces: tuple[LayoutSurface, ...] = Field(
        default=(),
        description="Located pages, slides, image frames, flows, or sheets. A usable result includes surfaces even for blank pages. Empty for failed/not_applicable outcomes.",
        examples=[
            [
                {
                    "id_": "page-1",
                    "kind": "page",
                    "location": {"page_number": 1},
                    "width": 612.0,
                    "height": 792.0,
                    "unit": "pt",
                }
            ],
            [],
        ],
    )
    regions: tuple[LayoutRegion, ...] = Field(
        default=(),
        description="Detected regions referencing returned surfaces. Empty can mean a blank page or a reported coverage gap; sequence alone does not establish reading order.",
        examples=[
            [
                {
                    "id": "page-1-r1",
                    "surface_id": "page-1",
                    "kind": "text",
                    "bbox": [48.0, 80.0, 300.0, 140.0],
                }
            ],
            [],
        ],
    )
    findings: tuple[LayoutFinding, ...] = Field(
        default=(),
        description="Limitations, skipped work, and failures reported by the analyzer. Required for partial/failed/not_applicable outcomes; may be empty on success.",
        examples=[
            [
                {
                    "code": "layout.pdf.object_limit",
                    "message": "Page regions were not analyzed: object limit exceeded.",
                    "surface_id": "page-1",
                }
            ],
            [],
        ],
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        surfaces = {surface.id_: surface for surface in self.surfaces}
        if len(surfaces) != len(self.surfaces):
            raise ValueError("surface IDs must be unique")
        if len({region.id for region in self.regions}) != len(self.regions):
            raise ValueError("region IDs must be unique")
        for region in self.regions:
            surface = surfaces.get(region.surface_id)
            if surface is None:
                raise ValueError("region must reference a returned surface")
            if surface.kind in ("page", "image"):
                if (
                    region.bbox is None
                    or surface.width is None
                    or surface.height is None
                ):
                    raise ValueError("page regions require geometry")
                if region.bbox[2] > surface.width or region.bbox[3] > surface.height:
                    raise ValueError("region bbox must fit its surface")
            elif surface.kind == "slide":
                if surface.width is None or surface.height is None:
                    raise ValueError("slides require dimensions")
                if (
                    not isinstance(region.location, PptxShapeLocation)
                    or not isinstance(surface.location, PptxSlideLocation)
                    or region.location.part != surface.location.part
                ):
                    raise ValueError(
                        "slide regions require a shape locator in the same part"
                    )
                if region.bbox is not None and (
                    region.bbox[2] > surface.width or region.bbox[3] > surface.height
                ):
                    raise ValueError("region bbox must fit its slide")
            elif surface.kind == "sheet":
                if (
                    region.bbox is not None
                    or not isinstance(
                        region.location, (XlsxRangeLocation, XlsxDrawingLocation)
                    )
                    or not isinstance(surface.location, XlsxSheetLocation)
                    or region.location.sheet_name != surface.location.sheet_name
                ):
                    raise ValueError(
                        "sheet regions require a locator in the same sheet and no bbox"
                    )
            elif isinstance(surface.location, HtmlDocumentLocation):
                if region.bbox is not None or not isinstance(
                    region.location, HtmlElementLocation
                ):
                    raise ValueError(
                        "HTML flow regions require an element locator and no bbox"
                    )
            elif (
                region.bbox is not None
                or not isinstance(region.location, DocxElementLocation)
                or not isinstance(surface.location, DocxBodyLocation)
                or region.location.part != surface.location.part
            ):
                raise ValueError(
                    "flow regions require a locator in the same part and no bbox"
                )
        if self.outcome in ("failed", "not_applicable") and (
            self.surfaces or self.regions
        ):
            raise ValueError(
                "failed/not-applicable results cannot contain a usable map"
            )
        if self.outcome in ("success", "partial") and not self.surfaces:
            raise ValueError("usable results require a surface, even for a blank page")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
