"""Pictures on PowerPoint slides, with python-pptx."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    R_EMBED,
    R_LINK,
    empty_region,
    failure,
    finish,
    nothing_selected,
    overlap,
    owners,
    package_names,
    read_source,
    select_image_regions,
    stored_picture,
    unresolved,
    unsupported_selection,
)
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    PptxShapeLocation,
)
from ...normalization.artifacts import Artifact
from ...shared.sources import as_artifact

# Graphic-frame contents that look like figures but are not stored pictures.
_FRAME_KINDS = ("/chart", "/diagram", "/ole")
# Package folders whose pictures are not on the slides themselves.
_OTHER_PARTS = (
    "/ppt/slideLayouts/",
    "/ppt/slideMasters/",
    "/ppt/notesSlides/",
    "/ppt/notesMasters/",
)


class PptxImageExtractor:
    """Return the pictures on a presentation's slides, with python-pptx.

    Without a layout: every picture shape on every slide, pictures inside
    groups and picture placeholders included, in storage order, with
    ``page_index`` the slide's zero-based position. A picture used on two
    slides comes back twice. Hidden slides are read, with ``hidden_slide``.
    ``alt_text`` is the picture's stored description.

    Each picture is the file stored in the presentation: PNG, JPEG, TIFF, GIF,
    and WebP byte for byte (``original``), other raster formats as new files
    (``reencoded``); EMF, WMF, and SVG pictures get ``unsupported_image_type``.
    A picture shown cropped on its slide comes back whole, with
    ``crop_not_applied``; a linked picture, stored outside the file, is not
    fetched (``reference_unresolved``).

    Not extracted, each with an informational finding: pictures on slide
    layouts and masters or in notes (``other_parts_unread``, once), and charts,
    diagrams, embedded objects, and pictures used as shape fills or slide
    backgrounds (``unsupported_content``, per slide).

    With a layout: the pictures in each requested shape region, located as
    ``PptxLayoutAnalyzer`` locates them. Without ``region_ids`` only image
    regions are used; explicit ``region_ids`` of any kind give the pictures in
    them (a group's members, for example). A chart, diagram, or embedded-object
    region gets ``selection_unsupported``; a region without a picture gets
    ``empty``. The innermost selected region owns a picture; regions locating
    the same shape are both refused (``selection_overlap``).
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.NOTHING_SELECTED,
            ImageFindingCode.OTHER_PARTS_UNREAD,
            ImageFindingCode.UNSUPPORTED_CONTENT,
            ImageFindingCode.REFERENCE_UNRESOLVED,
            ImageFindingCode.HIDDEN_SLIDE,
            ImageFindingCode.CROP_NOT_APPLIED,
        }
    )

    @property
    def name(self) -> str:
        return "image_extractor_pptx"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult:
        artifact = as_artifact(source)
        selected = select_image_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name, explicit=region_ids is not None)
        data = read_source(artifact, self.name)
        if isinstance(data, ImageExtractResult):
            return data
        names = package_names(data, self.name)
        if isinstance(names, ImageExtractResult):
            return names
        try:
            from lxml.etree import LxmlError
            from pptx import Presentation
            from pptx.exc import PackageNotFoundError
            from pptx.oxml.ns import qn
        except ImportError as error:
            raise ImportError(
                "PowerPoint image extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            presentation = Presentation(BytesIO(data))
            # Never use presentation.slides: it renames parts to match slide order,
            # and layout analysis locates shapes by the original part names.
            slide_ids = presentation.element.sldIdLst
            slides = [
                presentation.part.related_slide(slide_id.rId)
                for slide_id in (slide_ids if slide_ids is not None else ())
            ]
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            return failure(
                self.name,
                ImageFindingCode.DECODE_FAILED,
                "Source could not be opened as a presentation.",
            )
        tags = _Tags(qn)
        if selected is None:
            return self._standalone(presentation, slides, tags)
        return self._regions(slides, tags, selected)

    def _standalone(
        self, presentation: Any, slides: list[Any], tags: "_Tags"
    ) -> ImageExtractResult:
        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        found = 0
        for index, slide in enumerate(slides):
            if PptxImageExtractor._hidden(slide):
                findings.append(PptxImageExtractor._hidden_finding(index))
            pictures = [
                shape
                for _, shape in PptxImageExtractor._shapes(slide.shapes)
                if PptxImageExtractor._is_picture(shape, tags)
            ]
            found += len(pictures)
            for shape in pictures:
                PptxImageExtractor._extract_picture(
                    slide, shape, tags, index, None, images, findings
                )
            if PptxImageExtractor._unsupported(slide, tags):
                findings.append(
                    ImageExtractFinding(
                        code=ImageFindingCode.UNSUPPORTED_CONTENT,
                        message="Charts, diagrams, embedded objects, or picture fills "
                        "on this slide were not extracted.",
                        page_index=index,
                    )
                )
        if PptxImageExtractor._other_parts(presentation):
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.OTHER_PARTS_UNREAD,
                    message="Pictures on slide layouts and masters or in notes were "
                    "not extracted.",
                )
            )
        if not found:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY, message="No pictures on the slides."
                )
            )
        return finish(self.name, images, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, slides: list[Any], tags: "_Tags", selected: tuple[LayoutRegion, ...]
    ) -> ImageExtractResult:
        findings: list[ImageExtractFinding] = []
        by_part = {
            str(slide.part.partname).lstrip("/"): (index, slide)
            for index, slide in enumerate(slides)
        }
        resolved: list[tuple[LayoutRegion, int, Any, tuple[int, ...], list[Any]]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, PptxShapeLocation):
                findings.append(
                    unsupported_selection(region.id, "Selection is not a slide shape.")
                )
                continue
            if location.part not in by_part:
                findings.append(unresolved(region.id, None))
                continue
            index, slide = by_part[location.part]
            shape = PptxImageExtractor._resolve(slide.shapes, location.shape_path)
            if shape is None or shape.shape_id != location.shape_id:
                findings.append(unresolved(region.id, index))
                continue
            if PptxImageExtractor._frame_kind(shape, tags):
                findings.append(
                    unsupported_selection(
                        region.id,
                        "Charts, diagrams, and embedded objects are not stored pictures.",
                    )
                )
                continue
            pictures = [
                found
                for _, found in PptxImageExtractor._shapes([shape])
                if PptxImageExtractor._is_picture(found, tags)
            ]
            # Key each picture by slide and shape path, so ownership compares the
            # same shape across regions.
            keyed = [
                (index, PptxImageExtractor._path_of(slide.shapes, picture))
                for picture in pictures
            ]
            resolved.append((region, index, slide, tuple(location.shape_path), keyed))
        owned, refused = owners(
            [((index, *path), keyed) for _, index, _, path, keyed in resolved]
        )
        images: list[ExtractedImage] = []
        completed = 0
        hidden_reported: set[int] = set()
        for number, (region, index, slide, _, keyed) in enumerate(resolved):
            if number in refused:
                findings.append(overlap(region.id, index))
                continue
            completed += 1
            if PptxImageExtractor._hidden(slide) and index not in hidden_reported:
                hidden_reported.add(index)
                findings.append(PptxImageExtractor._hidden_finding(index))
            if not keyed:
                findings.append(empty_region(region.id, index))
            for _, path in owned[number]:
                shape = PptxImageExtractor._resolve(slide.shapes, path)
                PptxImageExtractor._extract_picture(
                    slide, shape, tags, index, region.id, images, findings
                )
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _shapes(
        shapes: Any, path: tuple[int, ...] = ()
    ) -> list[tuple[tuple[int, ...], Any]]:
        """Every shape, entering groups, in storage order, with its path of indexes."""
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        found: list[tuple[tuple[int, ...], Any]] = []
        pending = [(path + (index,), shape) for index, shape in enumerate(shapes)][::-1]
        while pending:
            where, shape = pending.pop()
            found.append((where, shape))
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                pending.extend(
                    [
                        (where + (index,), child)
                        for index, child in enumerate(shape.shapes)
                    ][::-1]
                )
        return found

    @staticmethod
    def _path_of(shapes: Any, target: Any) -> tuple[int, ...]:
        """The path of indexes from the slide to ``target``."""
        for where, shape in PptxImageExtractor._shapes(shapes):
            if shape.element is target.element:
                return where
        raise ValueError("shape not on slide")

    @staticmethod
    def _resolve(shapes: Any, path: Sequence[int]) -> Any:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        shape = None
        for depth, index in enumerate(path):
            if depth and (shape is None or shape.shape_type != MSO_SHAPE_TYPE.GROUP):
                return None
            members = list(shapes if shape is None else shape.shapes)
            if index >= len(members):
                return None
            shape = members[index]
        return shape

    @staticmethod
    def _is_picture(shape: Any, tags: "_Tags") -> bool:
        return shape.element.tag == tags.picture

    @staticmethod
    def _frame_kind(shape: Any, tags: "_Tags") -> bool:
        if shape.element.tag != tags.frame:
            return False
        data = next(shape.element.iter(tags.graphic_data), None)
        uri = "" if data is None else data.get("uri", "")
        return uri.endswith(_FRAME_KINDS)

    @staticmethod
    def _extract_picture(
        slide: Any,
        shape: Any,
        tags: "_Tags",
        index: int,
        region_id: str | None,
        images: list[ExtractedImage],
        findings: list[ImageExtractFinding],
    ) -> None:
        element = shape.element
        properties = next(element.iter(tags.properties), None)
        alt_text = None if properties is None else properties.get("descr")
        blip = next(element.iter(tags.blip), None)
        embedded = None if blip is None else blip.get(R_EMBED)
        linked = None if blip is None else blip.get(R_LINK)
        relation = slide.part.rels.get(embedded) if embedded else None
        if relation is None or relation.is_external:
            if linked or relation is not None:
                # Only a link (r:link), or an embed whose target is outside the file.
                code = ImageFindingCode.REFERENCE_UNRESOLVED
                message = "A linked picture is stored outside the presentation and was not fetched."
            else:
                code = ImageFindingCode.IMAGE_UNREADABLE
                message = "One picture's stored file was not found in the presentation."
            findings.append(
                ImageExtractFinding(
                    code=code, message=message, page_index=index, region_id=region_id
                )
            )
            return
        part = relation.target_part
        extracted = stored_picture(
            part.blob,
            page_index=index,
            region_id=region_id,
            alt_text=alt_text,
            content_type=part.content_type,
        )
        if isinstance(extracted, ExtractedImage):
            images.append(extracted)
            rect = next(element.iter(tags.source_rect), None)
            if rect is not None and any(
                int(rect.get(side, "0") or 0) for side in ("l", "t", "r", "b")
            ):
                findings.append(
                    ImageExtractFinding(
                        code=ImageFindingCode.CROP_NOT_APPLIED,
                        message="This picture is shown cropped on the slide; the whole "
                        "stored picture was returned.",
                        page_index=index,
                        region_id=region_id,
                    )
                )
        else:
            findings.append(extracted)

    @staticmethod
    def _unsupported(slide: Any, tags: "_Tags") -> bool:
        """Charts, diagrams, or objects on the slide, or pictures used as fills."""
        tree = slide.element
        if any(
            PptxImageExtractor._frame_kind(_Frame(frame), tags)
            for frame in tree.iter(tags.frame)
        ):
            return True
        for blip in tree.iter(tags.blip):
            ancestor = blip.getparent()
            while ancestor is not None and ancestor.tag not in (
                tags.picture,
                tags.frame,
            ):
                ancestor = ancestor.getparent()
            if ancestor is None:  # a picture fill on a shape, or the slide background
                return True
        return False

    @staticmethod
    def _hidden(slide: Any) -> bool:
        return slide.element.get("show") in ("0", "false")

    @staticmethod
    def _hidden_finding(index: int) -> ImageExtractFinding:
        return ImageExtractFinding(
            code=ImageFindingCode.HIDDEN_SLIDE,
            message="This slide is hidden in the presentation; it was read.",
            page_index=index,
        )

    @staticmethod
    def _other_parts(presentation: Any) -> bool:
        for part in presentation.part.package.iter_parts():
            name = str(part.partname)
            if name.startswith(_OTHER_PARTS) and b"blip" in part.blob:
                return True
        return False


class _Tags:
    """Clark names of the slide elements this extractor looks at."""

    def __init__(self, qn: Any) -> None:
        self.picture = qn("p:pic")
        self.blip = qn("a:blip")
        self.source_rect = qn("a:srcRect")
        self.properties = qn("p:cNvPr")
        self.frame = qn("p:graphicFrame")
        self.graphic_data = qn("a:graphicData")


class _Frame:
    """Just enough of a shape for ``_frame_kind`` on a raw graphic-frame element."""

    def __init__(self, element: Any) -> None:
        self.element = element
