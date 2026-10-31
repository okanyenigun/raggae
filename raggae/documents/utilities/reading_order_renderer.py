"""Prepare document images showing supplied reading-order suggestions."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .region_renderer import DocumentRegionRenderer
from ..normalization.artifacts import Artifact
from ..layout_analysis.models import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    PdfPageLocation,
)
from ..layout_analysis.map_analysis.reading_order.models import (
    ReadingOrderEdge,
    ReadingOrderResult,
)


class DocumentReadingOrderRenderer:
    """Draw returned links without inventing a reading sequence.

    Return detached RGB images keyed by one-based PDF page or image frame.
    Displaying, saving and closing them are the caller's responsibility.
    Numbers are local to a page/frame and appear only when every region has
    one unambiguous sequence. Explicitly unresolved regions are red with '?'.
    Cross-page/frame edges are not drawn across separate images.
    """

    _COLORS = {
        "text": "#2563EB",
        "heading": "#BE185D",
        "caption": "#0F766E",
        "table": "#15803D",
        "image": "#C2410C",
        "unknown": "#7C3AED",
    }

    def __init__(
        self,
        *,
        dpi: int = 96,
        show_labels: bool = True,
        max_pixels_per_page: int = 20_000_000,
    ) -> None:
        self._renderer = DocumentRegionRenderer(
            dpi=dpi,
            show_labels=False,
            max_pixels_per_page=max_pixels_per_page,
        )
        self._show_labels = show_labels

    def render(
        self,
        source: Path | str | bytes | Artifact,
        layout: LayoutAnalysisResult,
        order: ReadingOrderResult,
    ) -> dict[int, Image.Image]:
        """Draw an existing map and order on the exact source they describe.

        This utility does not discover regions, resolve order, display images,
        or write files. PDF rendering needs the layout-visual extra. Native
        Word/HTML/worksheet/slide maps without a supported renderer are rejected.
        Invalid references are rejected before the source is opened. If drawing
        fails, all prepared images are closed instead of returning partial output.
        """
        if not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult")
        if not isinstance(order, ReadingOrderResult):
            raise TypeError("order must be a ReadingOrderResult")
        if order.outcome in {"failed", "not_applicable"}:
            raise ValueError("order must contain a usable reading-order result")
        identifiers = {region.id for region in layout.regions}
        unresolved = set(order.unresolved_region_ids)
        referenced = unresolved | {
            identifier
            for edge in order.edges
            for identifier in (edge.before_region_id, edge.after_region_id)
        }
        if referenced - identifiers:
            raise ValueError("order references region IDs absent from the layout")
        if any(region.bbox is None for region in layout.regions):
            raise ValueError("reading-order rendering requires region rectangles")

        # Reuse source handling, passwords, crop/rotation, dimension checks,
        # pixel limits and bitmap cleanup through the renderer's public API.
        images = self._renderer.render(source, layout=layout)
        try:
            for surface in layout.surfaces:
                location = surface.location
                assert isinstance(location, (PdfPageLocation, ImageFrameLocation))
                number = (
                    location.page_number
                    if isinstance(location, PdfPageLocation)
                    else location.frame_number
                )
                assert surface.width is not None and surface.height is not None
                regions = {
                    region.id: region
                    for region in layout.regions
                    if region.surface_id == surface.id_
                }
                edges = tuple(
                    edge
                    for edge in order.edges
                    if edge.before_region_id in regions
                    and edge.after_region_id in regions
                )
                ranks = self._ranks(tuple(regions), edges, unresolved)
                self._draw_order(
                    images[number],
                    (surface.width, surface.height),
                    regions,
                    edges,
                    unresolved,
                    ranks,
                )
        except BaseException:
            for image in images.values():
                image.close()
            raise
        return images

    @staticmethod
    def _ranks(
        identifiers: tuple[str, ...],
        edges: tuple[ReadingOrderEdge, ...],
        unresolved: set[str],
    ) -> dict[str, int]:
        """Number only a complete unique local order, never break a tie."""
        if unresolved.intersection(identifiers):
            return {}
        outgoing: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
        incoming = dict.fromkeys(identifiers, 0)
        for edge in edges:
            outgoing[edge.before_region_id].append(edge.after_region_id)
            incoming[edge.after_region_id] += 1
        ready = [identifier for identifier in identifiers if incoming[identifier] == 0]
        sequence = []
        while len(ready) == 1:
            identifier = ready.pop()
            sequence.append(identifier)
            for following in outgoing[identifier]:
                incoming[following] -= 1
                if incoming[following] == 0:
                    ready.append(following)
        if len(sequence) != len(identifiers):
            return {}
        return {identifier: number for number, identifier in enumerate(sequence, 1)}

    def _draw_order(
        self,
        image: Image.Image,
        size: tuple[float, float],
        regions: dict[str, LayoutRegion],
        edges: tuple[ReadingOrderEdge, ...],
        unresolved: set[str],
        ranks: dict[str, int],
    ) -> None:
        sx, sy = image.width / size[0], image.height / size[1]
        boxes = {}
        for identifier, region in regions.items():
            assert region.bbox is not None
            left, top, right, bottom = region.bbox
            boxes[identifier] = (
                min(image.width - 1, left * sx),
                min(image.height - 1, top * sy),
                min(image.width - 1, right * sx),
                min(image.height - 1, bottom * sy),
            )
        centers = {
            identifier: ((left + right) / 2, (top + bottom) / 2)
            for identifier, (left, top, right, bottom) in boxes.items()
        }
        draw = ImageDraw.Draw(image)
        for edge in edges:
            start = centers[edge.before_region_id]
            end = centers[edge.after_region_id]
            draw.line((start, end), fill="#94A3B8", width=2)
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = (dx * dx + dy * dy) ** 0.5
            if length:
                ux, uy = dx / length, dy / length
                draw.polygon(
                    (
                        end,
                        (end[0] - 10 * ux + 4 * uy, end[1] - 10 * uy - 4 * ux),
                        (end[0] - 10 * ux - 4 * uy, end[1] - 10 * uy + 4 * ux),
                    ),
                    fill="#64748B",
                )
        font = ImageFont.load_default(size=12)
        for identifier, region in regions.items():
            color = "#DC2626" if identifier in unresolved else self._COLORS[region.kind]
            box = boxes[identifier]
            # Redraw borders above arrows, and apply the unresolved color.
            draw.rectangle(box, outline=color, width=2)
            if not self._show_labels:
                continue
            prefix = (
                f"{ranks[identifier]}. "
                if identifier in ranks
                else "? "
                if identifier in unresolved
                else ""
            )
            label = f"{prefix}{identifier} {region.kind}"
            a, b, c, d = draw.textbbox((0, 0), label, font=font)
            x = max(0.0, min(box[0], image.width - (c - a)))
            y = max(0.0, box[1] - (d - b) - 4)
            position = (x - a, y - b)
            draw.rectangle(draw.textbbox(position, label, font=font), fill="white")
            draw.text(position, label, font=font, fill=color)
