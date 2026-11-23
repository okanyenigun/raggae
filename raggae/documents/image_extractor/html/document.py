"""Pictures written inside an HTML document, with BeautifulSoup."""

import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    empty_region,
    failure,
    finish,
    nothing_selected,
    overlap,
    owners,
    picture_at,
    read_source,
    select_image_regions,
    unresolved,
    unsupported_selection,
)
from ...layout_analysis.models import (
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding
from ...sources import as_artifact

# Never read: executable, styling, inert, or document-metadata elements (as the
# HTML text reader). Not pictures: vector, embedded, or interactive elements.
_SKIPPED = frozenset({"script", "style", "template", "noscript", "head", "title"})
_UNSUPPORTED = frozenset({"svg", "iframe", "object", "embed", "canvas", "video"})


class HtmlImageExtractor:
    """Return the pictures written inside an HTML document, with BeautifulSoup.

    Every ``<img>`` is found in document order. A picture written inside the
    file as a data URL (``src="data:image/png;base64,..."``) comes back as its
    file: PNG, JPEG, TIFF, GIF, and WebP byte for byte (``original``), other
    raster formats as new files (``reencoded``), SVG as
    ``unsupported_image_type``. ``alt_text`` is the ``alt`` attribute as
    written (an empty ``alt`` marks a decorative picture), and ``page_index``
    is always 0.

    A picture in another file (``src="photos/pump.png"``, ``https://...``) is
    never opened or fetched; ``<base>`` grants nothing. An informational
    ``reference_unresolved`` names it, without user names, passwords, queries,
    or fragments. Alternative sources (``srcset``, ``<picture>``) are not read:
    each ``<img>``'s own ``src`` is.

    As in the HTML text reader: scripts, styles, ``<template>``,
    ``<noscript>``, and ``<head>`` are never read; elements with the ``hidden``
    attribute are skipped (``hidden_skipped`` when they hold pictures; CSS is
    not applied); inline SVG, canvas, video, and embedded objects or frames are
    not pictures (``unsupported_content``). The source is decoded with
    ``encoding`` (UTF-8 by default), never guessed from ``<meta charset>``.

    With a layout: the pictures at or inside each requested element, located
    as ``HtmlLayoutAnalyzer`` locates them (element-only child indexes, tag
    checked). Without ``region_ids`` only image regions are used; explicit
    ``region_ids`` of any kind give the pictures inside them. The innermost
    selected region owns a picture; regions locating the same element are both
    refused (``selection_overlap``). A hidden region is counted, with
    ``hidden_skipped``; a region in never-read content or on a non-picture
    element gets ``selection_unsupported``; one without a picture, ``empty``.
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.NOTHING_SELECTED,
            ImageFindingCode.REFERENCE_UNRESOLVED,
            ImageFindingCode.UNSUPPORTED_CONTENT,
            ImageFindingCode.HIDDEN_SKIPPED,
        }
    )

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "image_extractor_html"

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
        data = read_source(artifact, self.name, allow_empty=True)
        if isinstance(data, ImageExtractResult):
            return data
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return failure(
                self.name,
                ImageFindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise ImportError(
                "HTML image extraction requires: pip install 'raggae[text-html]'"
            ) from error
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                document = BeautifulSoup(text, "html.parser")
            if selected is None:
                return self._standalone(document)
            return self._regions(document, selected)
        except RecursionError:
            return failure(
                self.name,
                ImageFindingCode.STRUCTURE_UNSUPPORTED,
                "HTML nesting is too deep to read within Python's recursion limit.",
            )
        except (ValueError, AssertionError):
            return failure(
                self.name,
                ImageFindingCode.PARSE_FAILED,
                "Source could not be parsed as HTML.",
            )

    def _standalone(self, document: Any) -> ImageExtractResult:
        pictures, hidden, unsupported = HtmlImageExtractor._pictures(document)
        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        for picture in pictures:
            HtmlImageExtractor._extract(picture, None, images, findings)
        if hidden:
            findings.append(HtmlImageExtractor._hidden(None))
        if unsupported:
            findings.append(HtmlImageExtractor._unsupported_content())
        if not pictures and not hidden:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY, message="No pictures in the document."
                )
            )
        return finish(self.name, images, findings, 1, self._INFORMATIONAL)

    def _regions(
        self, document: Any, selected: tuple[LayoutRegion, ...]
    ) -> ImageExtractResult:
        findings: list[ImageExtractFinding] = []
        resolved: list[tuple[LayoutRegion, tuple[int, ...], list[Any], bool]] = []
        unsupported_inside = False
        completed = 0
        for region in selected:
            location = region.location
            if not isinstance(location, HtmlElementLocation):
                findings.append(
                    unsupported_selection(
                        region.id, "Selection is not an HTML element."
                    )
                )
                continue
            element = HtmlImageExtractor._resolve(document, location.child_indexes)
            if element is None or element.name != location.tag:
                findings.append(unresolved(region.id, 0))
                continue
            lineage = [element, *element.parents]
            if element.name in _UNSUPPORTED or any(
                node.name in _SKIPPED for node in lineage
            ):
                findings.append(
                    unsupported_selection(
                        region.id,
                        "The region is never-read content or an element that is not "
                        "a picture.",
                    )
                )
                continue
            if any(HtmlImageExtractor._is_hidden(node) for node in lineage):
                completed += 1
                findings.append(HtmlImageExtractor._hidden(region.id))
                continue
            pictures, hidden, unsupported = HtmlImageExtractor._pictures(element)
            unsupported_inside = unsupported_inside or unsupported
            resolved.append((region, tuple(location.child_indexes), pictures, hidden))
        by_id = {id(p): p for _, _, pictures, _ in resolved for p in pictures}
        owned, refused = owners(
            [(path, [id(p) for p in pictures]) for _, path, pictures, _ in resolved]
        )
        images: list[ExtractedImage] = []
        for number, (region, _, pictures, hidden) in enumerate(resolved):
            if number in refused:
                findings.append(overlap(region.id, 0))
                continue
            completed += 1
            if hidden:
                findings.append(HtmlImageExtractor._hidden(region.id))
            elif not pictures:
                findings.append(empty_region(region.id, 0))
            for key in owned[number]:
                HtmlImageExtractor._extract(by_id[key], region.id, images, findings)
        if unsupported_inside:
            findings.append(HtmlImageExtractor._unsupported_content())
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _pictures(node: Any) -> tuple[list[Any], bool, bool]:
        """The ``<img>`` elements at or under ``node`` that are read, in order;
        whether hidden ones were skipped; and whether non-picture elements were met."""
        from bs4.element import Tag

        pictures: list[Any] = []
        hidden = unsupported = False
        pending = [node]
        while pending:
            item = pending.pop()
            if not isinstance(item, Tag) or item.name in _SKIPPED:
                continue
            if HtmlImageExtractor._is_hidden(item):
                hidden = hidden or item.name == "img" or item.find("img") is not None
                continue
            if item.name in _UNSUPPORTED:
                unsupported = True
                continue
            if item.name == "img":
                pictures.append(item)
                continue
            pending.extend(
                reversed([child for child in item.children if isinstance(child, Tag)])
            )
        return pictures, hidden, unsupported

    @staticmethod
    def _extract(
        picture: Any,
        region_id: str | None,
        images: list[ExtractedImage],
        findings: list[ImageExtractFinding],
    ) -> None:
        source = picture.get("src")
        alt_text = picture.get("alt")
        extracted = picture_at(
            source if isinstance(source, str) else "",
            alt_text=alt_text if isinstance(alt_text, str) else None,
        )
        extracted = extracted.model_copy(update={"region_id": region_id})
        if isinstance(extracted, ExtractedImage):
            images.append(extracted)
        else:
            findings.append(extracted)

    @staticmethod
    def _resolve(document: Any, indexes: Sequence[int]) -> Any:
        from bs4.element import Tag

        node = document
        for index in indexes:
            children = [child for child in node.children if isinstance(child, Tag)]
            if index >= len(children):
                return None
            node = children[index]
        return node

    @staticmethod
    def _is_hidden(node: Any) -> bool:
        return hasattr(node, "has_attr") and node.has_attr("hidden")

    @staticmethod
    def _hidden(region_id: str | None) -> ImageExtractFinding:
        return ImageExtractFinding(
            code=ImageFindingCode.HIDDEN_SKIPPED,
            message="Pictures in hidden elements were not extracted.",
            region_id=region_id,
            page_index=0 if region_id else None,
        )

    @staticmethod
    def _unsupported_content() -> ImageExtractFinding:
        return ImageExtractFinding(
            code=ImageFindingCode.UNSUPPORTED_CONTENT,
            message="Inline SVG, canvas, video, and embedded objects or frames are not "
            "pictures and were not extracted.",
        )
