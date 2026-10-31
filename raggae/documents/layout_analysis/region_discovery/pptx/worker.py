"""Locate native slide shapes; source stacking order is not reading order."""

from pathlib import Path
from io import BytesIO
from math import isfinite
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from .policy import PptxLayoutPolicy
from ...models import (
    BoundingBox,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PptxShapeLocation,
    PptxSlideLocation,
    RegionKind,
)
from ...utils import resolve_source
from ....normalization.artifacts import Artifact

type Transform = tuple[float, float, float, float]


class PptxLayoutAnalyzer:
    """Map direct slide text, tables and pictures, including supported groups."""

    def __init__(self, policy: PptxLayoutPolicy | None = None) -> None:
        self._policy = policy if policy is not None else PptxLayoutPolicy()

    @property
    def policy(self) -> PptxLayoutPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_pptx"

    def analyze(self, source: Path | str | bytes | Artifact) -> LayoutAnalysisResult:
        source = resolve_source(source)
        try:
            if source.path is not None:
                path = source.path
                info = path.stat()
                if not S_ISREG(info.st_mode):
                    return self._failure(
                        "source_unreadable", "Source must be a regular file."
                    )
                if info.st_size > self._policy.max_source_bytes:
                    return self._failure(
                        "source_too_large", "Source exceeds the byte limit."
                    )
                with path.open("rb") as stream:
                    data = stream.read(self._policy.max_source_bytes + 1)
            else:
                assert source.data is not None
                data = source.data
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure("source_too_large", "Source exceeds the byte limit.")
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > self._policy.max_archive_entries
                    or sum(entry.file_size for entry in entries)
                    > self._policy.max_expanded_bytes
                    or any(entry.flag_bits & 1 for entry in entries)
                ):
                    return self._failure(
                        "archive_limit",
                        "Package exceeds ZIP limits or contains encrypted ZIP entries.",
                    )
                if len({entry.filename for entry in entries}) != len(entries):
                    return self._failure(
                        "decode_failed", "Package contains duplicate part names."
                    )
        except BadZipFile:
            return self._failure(
                "decode_failed", "Source is not a readable presentation package."
            )
        try:
            from pptx import Presentation
            from pptx.exc import PackageNotFoundError
            from lxml.etree import LxmlError
        except ImportError as error:
            raise ImportError(
                "Slide layout requires: pip install 'raggae[layout-pptx]'"
            ) from error
        try:
            with BytesIO(data) as stream:
                presentation = Presentation(stream)
        except (BadZipFile, PackageNotFoundError, LxmlError, ValueError, KeyError):
            return self._failure(
                "decode_failed", "Source could not be opened as a presentation."
            )
        slide_width, slide_height = presentation.slide_width, presentation.slide_height
        if (
            slide_width is None
            or slide_height is None
            or slide_width <= 0
            or slide_height <= 0
        ):
            return self._failure(
                "invalid_dimensions", "Presentation requires positive slide dimensions."
            )
        width, height = float(slide_width) / 12700, float(slide_height) / 12700
        surfaces, regions, findings = [], [], []
        # Presentation.slides renames parts to match slide order. Resolve the
        # ordered relationships directly so locators retain source package paths.
        slide_ids = presentation.element.sldIdLst
        for number, slide_id in enumerate(
            slide_ids if slide_ids is not None else (), 1
        ):
            if number > self._policy.max_slides:
                findings.append(
                    LayoutFinding(
                        code="layout.pptx.slide_limit",
                        message="Remaining slides were not analyzed: slide limit reached.",
                    )
                )
                break
            slide = presentation.part.related_slide(slide_id.rId)
            surface = LayoutSurface(
                id_=f"slide-{number}",
                kind="slide",
                location=PptxSlideLocation(
                    slide_number=number, part=str(slide.part.partname).lstrip("/")
                ),
                width=width,
                height=height,
            )
            surfaces.append(surface)
            self._map_slide(slide, surface, regions, findings)
        if not surfaces:
            return self._failure("no_slides", "Presentation contains no slides.")
        return LayoutAnalysisResult(
            outcome="partial" if findings else "success",
            surfaces=tuple(surfaces),
            regions=tuple(regions),
            findings=tuple(findings),
        )

    def _map_slide(
        self,
        slide: Any,
        surface: LayoutSurface,
        regions: list[LayoutRegion],
        findings: list[LayoutFinding],
    ) -> None:
        from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
        from pptx.oxml.ns import qn

        assert isinstance(surface.location, PptxSlideLocation)
        assert surface.width is not None and surface.height is not None
        pending: list[tuple[Any, tuple[int, ...], Transform]] = [
            (shape, (index,), (1, 1, 0, 0))
            for index, shape in reversed(list(enumerate(slide.shapes)))
        ]
        count = 0
        while pending:
            if count >= self._policy.max_shapes_per_slide:
                findings.append(
                    LayoutFinding(
                        code="layout.pptx.shape_limit",
                        surface_id=surface.id_,
                        message="Remaining slide shapes were not analyzed: shape limit reached.",
                    )
                )
                break
            shape, path, transform = pending.pop()
            count += 1
            issue = None
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                if not len(shape.shapes):
                    continue
                try:
                    if len(path) >= self._policy.max_group_depth:
                        raise ValueError("group depth limit")
                    child_transform = self._group_transform(shape, transform)
                except (ValueError, TypeError, AttributeError):
                    issue = "group_transform_unresolved"
                else:
                    pending.extend(
                        (child, path + (index,), child_transform)
                        for index, child in reversed(list(enumerate(shape.shapes)))
                    )
                    continue
            kind: RegionKind = "unknown"
            if issue is None:
                if shape.has_table:
                    kind = "table"
                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    kind = "image"
                    if any(
                        blip.get(qn("r:link"))
                        for blip in shape.element.iter(qn("a:blip"))
                    ):
                        issue = "linked_image"
                elif shape.has_text_frame:
                    if not shape.text_frame.text.strip():
                        if (
                            shape.is_placeholder
                            or shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX
                        ):
                            continue
                    else:
                        kind = "text"
                        if shape.is_placeholder and shape.placeholder_format.type in (
                            PP_PLACEHOLDER.TITLE,
                            PP_PLACEHOLDER.CENTER_TITLE,
                            PP_PLACEHOLDER.VERTICAL_TITLE,
                        ):
                            kind = "heading"
                if kind == "unknown":
                    issue = "unsupported_shape"
            bbox = None
            if issue != "group_transform_unresolved":
                try:
                    raw = self._shape_box(shape, transform)
                    clipped = (
                        max(0.0, raw[0]),
                        max(0.0, raw[1]),
                        min(surface.width, raw[2]),
                        min(surface.height, raw[3]),
                    )
                    if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
                        bbox = clipped
                    if bbox != raw:
                        issue = issue or "shape_clipped"
                except (ValueError, TypeError, AttributeError):
                    issue = issue or "shape_geometry_unresolved"
            identifier = f"{surface.id_}-s{'-'.join(map(str, path))}"
            regions.append(
                LayoutRegion(
                    id=identifier,
                    surface_id=surface.id_,
                    kind=kind,
                    bbox=bbox,
                    location=PptxShapeLocation(
                        part=surface.location.part,
                        shape_id=shape.shape_id,
                        shape_path=path,
                    ),
                )
            )
            if issue:
                findings.append(
                    LayoutFinding(
                        code=f"layout.pptx.{issue}",
                        surface_id=surface.id_,
                        region_id=identifier,
                        message="Shape location was retained; geometry, content, or visible coverage requires additional interpretation.",
                    )
                )
        inherited = list(slide.slide_layout.shapes) + list(
            slide.slide_layout.slide_master.shapes
        )
        if any(not shape.is_placeholder for shape in inherited):
            findings.append(
                LayoutFinding(
                    code="layout.pptx.inherited_content",
                    surface_id=surface.id_,
                    message="Layout/master decorations were not mapped as direct slide regions.",
                )
            )
        if (
            slide.has_notes_slide
            and slide.notes_slide.notes_text_frame is not None
            and slide.notes_slide.notes_text_frame.text.strip()
        ):
            findings.append(
                LayoutFinding(
                    code="layout.pptx.notes_unanalyzed",
                    surface_id=surface.id_,
                    message="Speaker notes were not analyzed.",
                )
            )
        if slide.element.get("show") == "0" or any(
            slide.element.find(qn(tag)) is not None
            for tag in ("p:timing", "p:transition")
        ):
            findings.append(
                LayoutFinding(
                    code="layout.pptx.presentation_behavior",
                    surface_id=surface.id_,
                    message="Hidden-slide, animation, or transition behavior was not interpreted.",
                )
            )

    @staticmethod
    def _group_transform(shape: Any, parent: Transform) -> Transform:
        """Compose group offset, scale and flips with the enclosing transform."""
        xfrm = shape.element.xfrm
        if xfrm is None or shape.rotation % 360:
            raise ValueError("group rotation or missing transform")
        if (
            xfrm.chOff is None
            or xfrm.chExt is None
            or xfrm.chExt.cx <= 0
            or xfrm.chExt.cy <= 0
        ):
            raise ValueError("group child extent is missing or empty")
        width, height = float(shape.width), float(shape.height)
        sx, sy = width / xfrm.chExt.cx, height / xfrm.chExt.cy
        tx, ty = float(shape.left), float(shape.top)
        if xfrm.flipH:
            sx, tx = -sx, tx + width
        if xfrm.flipV:
            sy, ty = -sy, ty + height
        tx, ty = tx - sx * xfrm.chOff.x, ty - sy * xfrm.chOff.y
        px, py, ox, oy = parent
        result = px * sx, py * sy, px * tx + ox, py * ty + oy
        if not all(isfinite(value) for value in result) or width <= 0 or height <= 0:
            raise ValueError("invalid group geometry")
        return result

    @staticmethod
    def _shape_box(shape: Any, transform: Transform) -> BoundingBox:
        """Apply the slide transform and convert internal EMUs to points."""
        if shape.rotation % 360:
            raise ValueError("rotated shape geometry is not supported")
        x, y, width, height = map(
            float, (shape.left, shape.top, shape.width, shape.height)
        )
        sx, sy, tx, ty = transform
        xs = sorted((sx * x + tx, sx * (x + width) + tx))
        ys = sorted((sy * y + ty, sy * (y + height) + ty))
        box = xs[0] / 12700, ys[0] / 12700, xs[1] / 12700, ys[1] / 12700
        if not all(isfinite(value) for value in box) or width <= 0 or height <= 0:
            raise ValueError("empty or invalid shape extent")
        return box

    @staticmethod
    def _failure(code: str, message: str) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            outcome="failed",
            findings=(LayoutFinding(code=f"layout.pptx.{code}", message=message),),
        )
