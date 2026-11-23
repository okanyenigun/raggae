"""Pictures in a Word document's main body, with python-docx."""

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
    MC_FALLBACK,
    R_EMBED,
    R_ID,
    R_LINK,
    VML_IMAGEDATA,
    VML_SHAPE,
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
    walk,
)
from ...layout_analysis.models import (
    DocxElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...normalization.artifacts import Artifact
from ...sources import as_artifact

# Parts whose pictures are outside the main body.
_OTHER_PARTS = frozenset({"header", "footer", "footnotes", "endnotes", "comments"})


class DocxImageExtractor:
    """Return the pictures drawn in a Word document's main body, with python-docx.

    Without a layout: one picture for every place the body shows one, in body
    order: inline and floating pictures, pictures in tables and text boxes,
    and legacy (VML) pictures. A picture used twice comes back twice. Every
    picture has ``page_index=0``, since a .docx stores no pages. Alternate
    content is read once (its ``mc:Choice`` copy), and pictures in deleted
    tracked content are left out, so the body reads as if changes were
    accepted. ``alt_text`` is the description Word stores (DrawingML ``descr``,
    VML ``alt``).

    Each picture is the file stored in the document: PNG, JPEG, TIFF, GIF, and
    WebP byte for byte (``original``), other raster formats as new files
    (``reencoded``). EMF, WMF, and SVG pictures get ``unsupported_image_type``;
    a linked picture, stored outside the file, is not fetched
    (``reference_unresolved``).

    Not extracted, each with one informational finding: pictures in headers,
    footers, footnotes, endnotes, or comments (``other_parts_unread``), and
    drawings that are not pictures, such as charts, shapes, and embedded
    objects (``unsupported_content``).

    With a layout: the pictures inside each requested region, located as
    ``DocxLayoutAnalyzer`` locates them. Without ``region_ids`` only image
    regions are used; explicit ``region_ids`` of any kind (a paragraph, a
    table) give the pictures inside them. The innermost selected region owns a
    picture; regions locating the same element are both refused
    (``selection_overlap``). A region without a picture gets ``empty``.
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.NOTHING_SELECTED,
            ImageFindingCode.OTHER_PARTS_UNREAD,
            ImageFindingCode.UNSUPPORTED_CONTENT,
            ImageFindingCode.REFERENCE_UNRESOLVED,
        }
    )

    @property
    def name(self) -> str:
        return "image_extractor_docx"

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
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
            from docx.oxml.ns import qn
            from lxml.etree import LxmlError
        except ImportError as error:
            raise ImportError(
                "Word image extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            document = Document(BytesIO(data))
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            # python-docx raises these on some malformed package parts.
            AttributeError,
            TypeError,
        ):
            return failure(
                self.name,
                ImageFindingCode.DECODE_FAILED,
                "Source could not be opened as a Word document.",
            )
        tags = _Tags(qn)
        body = document.element.body
        if selected is None:
            return self._standalone(document, body, tags)
        return self._regions(document, body, tags, selected)

    def _standalone(
        self, document: Any, body: Any, tags: "_Tags"
    ) -> ImageExtractResult:
        drawings, unsupported = DocxImageExtractor._drawings(body, tags)
        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        for drawing in drawings:
            DocxImageExtractor._extract_drawing(
                document, drawing, tags, None, images, findings
            )
        findings.extend(DocxImageExtractor._document_findings(document, unsupported))
        if not drawings:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No pictures in the document body.",
                )
            )
        return finish(self.name, images, findings, 1, self._INFORMATIONAL)

    def _regions(
        self,
        document: Any,
        body: Any,
        tags: "_Tags",
        selected: tuple[LayoutRegion, ...],
    ) -> ImageExtractResult:
        findings: list[ImageExtractFinding] = []
        part = str(document.part.partname).lstrip("/")
        resolved: list[tuple[LayoutRegion, tuple[int, ...], list[Any]]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, DocxElementLocation):
                findings.append(
                    unsupported_selection(region.id, "Selection is not a Word element.")
                )
                continue
            node = (
                DocxImageExtractor._resolve(body, location.child_indexes)
                if location.part == part
                else None
            )
            if node is None:
                findings.append(unresolved(region.id, 0))
                continue
            resolved.append(
                (
                    region,
                    tuple(location.child_indexes),
                    DocxImageExtractor._drawings(node, tags)[0],
                )
            )
        owned, refused = owners([(path, drawings) for _, path, drawings in resolved])
        images: list[ExtractedImage] = []
        completed = 0
        for number, (region, _, drawings) in enumerate(resolved):
            if number in refused:
                findings.append(overlap(region.id, 0))
                continue
            completed += 1
            if not drawings:
                findings.append(empty_region(region.id, 0))
            for drawing in owned[number]:
                DocxImageExtractor._extract_drawing(
                    document, drawing, tags, region.id, images, findings
                )
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _drawings(node: Any, tags: "_Tags") -> tuple[list[Any], bool]:
        """Picture-bearing drawings under ``node`` in order, and whether others were met."""
        drawings: list[Any] = []
        unsupported = False
        for element in walk(node, tags.skip, tags.stop):
            if element.tag == tags.object:
                unsupported = True
            elif element.tag in (tags.drawing, tags.pict):
                if any(True for _ in element.iter(tags.blip, VML_IMAGEDATA)):
                    drawings.append(element)
                else:
                    unsupported = True
        return drawings, unsupported

    @staticmethod
    def _extract_drawing(
        document: Any,
        drawing: Any,
        tags: "_Tags",
        region_id: str | None,
        images: list[ExtractedImage],
        findings: list[ImageExtractFinding],
    ) -> None:
        """Each picture in one drawing: a stored file, or a finding."""
        if drawing.tag == tags.drawing:
            properties = next(drawing.iter(tags.doc_pr), None)
            alt_text = None if properties is None else properties.get("descr")
            references = [
                (blip.get(R_EMBED), blip.get(R_LINK))
                for blip in drawing.iter(tags.blip)
            ]
        else:
            shape = next(drawing.iter(VML_SHAPE), None)
            alt_text = None if shape is None else shape.get("alt")
            references = [
                (data.get(R_ID), None) for data in drawing.iter(VML_IMAGEDATA)
            ]
        for embedded, linked in references:
            relation = document.part.rels.get(embedded) if embedded else None
            if relation is not None and not relation.is_external:
                part = relation.target_part
                extracted = stored_picture(
                    part.blob,
                    page_index=0,
                    region_id=region_id,
                    alt_text=alt_text,
                    content_type=part.content_type,
                )
                if isinstance(extracted, ExtractedImage):
                    images.append(extracted)
                else:
                    findings.append(extracted)
            elif linked or relation is not None:
                # Only a link (r:link), or an embed whose target is outside the file.
                findings.append(
                    ImageExtractFinding(
                        code=ImageFindingCode.REFERENCE_UNRESOLVED,
                        message="A linked picture is stored outside the document and "
                        "was not fetched.",
                        page_index=0,
                        region_id=region_id,
                    )
                )
            else:
                findings.append(
                    ImageExtractFinding(
                        code=ImageFindingCode.IMAGE_UNREADABLE,
                        message="One picture's stored file was not found in the document.",
                        page_index=0,
                        region_id=region_id,
                    )
                )

    @staticmethod
    def _document_findings(
        document: Any, unsupported: bool
    ) -> list[ImageExtractFinding]:
        findings: list[ImageExtractFinding] = []
        if any(
            not relation.is_external
            and relation.reltype.rsplit("/", 1)[-1] in _OTHER_PARTS
            and (
                b"blip" in relation.target_part.blob
                or b"imagedata" in relation.target_part.blob
            )
            for relation in document.part.rels.values()
        ):
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.OTHER_PARTS_UNREAD,
                    message="Pictures in headers, footers, notes, or comments were not extracted.",
                )
            )
        if unsupported:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.UNSUPPORTED_CONTENT,
                    message="Drawings that are not pictures (charts, shapes, embedded "
                    "objects) were not extracted.",
                )
            )
        return findings

    @staticmethod
    def _resolve(body: Any, indexes: Sequence[int]) -> Any:
        node = body
        for index in indexes:
            children = list(node)
            if index >= len(children):
                return None
            node = children[index]
        return node


class _Tags:
    """Clark names of the Word elements this extractor looks at."""

    def __init__(self, qn: Any) -> None:
        self.drawing = qn("w:drawing")
        self.pict = qn("w:pict")
        self.object = qn("w:object")
        self.blip = qn("a:blip")
        self.doc_pr = qn("wp:docPr")
        self.skip = frozenset({MC_FALLBACK, qn("w:del"), qn("w:moveFrom")})
        self.stop = frozenset({self.drawing, self.pict, self.object})
