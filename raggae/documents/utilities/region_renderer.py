"""Draw supplied document regions on PDF pages or decoded image frames."""

import pikepdf
from collections.abc import Mapping, Sequence
from contextlib import closing
from io import BytesIO
from math import ceil, isclose, isfinite
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw, ImageFont
from stat import S_ISREG
from typing import TYPE_CHECKING
from ..normalization.artifacts import Artifact

if TYPE_CHECKING:
    from ..layout_analysis.models import LayoutAnalysisResult


class DocumentRegionRenderer:
    """Prepare annotated Pillow images without displaying or saving them.

    Call render(source, boxes={1: [(left, top, right, bottom)]}) for independent
    use, or render(source, layout=result) to use a layout analysis result.
    Both return dict[int, Image.Image], keyed by one-based page/frame number.
    PDF boxes use displayed-crop top-left points; image boxes use decoded pixels.
    Only selected pages/frames are rendered; {1: []} renders a page without boxes.
    Labels and colors, when supplied, must match the boxes on each selected page.
    """

    _LAYOUT_COLORS = {
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
        for name, value in (("dpi", dpi), ("max_pixels_per_page", max_pixels_per_page)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        self._dpi = dpi
        self._show_labels = show_labels
        self._max_pixels_per_page = max_pixels_per_page

    def render(
        self,
        source: Path | str | bytes | Artifact,
        layout: "LayoutAnalysisResult | None" = None,
        *,
        boxes: Mapping[int, Sequence[tuple[float, float, float, float]]] | None = None,
        labels: Mapping[int, Sequence[str]] | None = None,
        colors: Mapping[int, Sequence[str]] | None = None,
    ) -> dict[int, Image.Image]:
        """Render one layout result or plain boxes, with optional labels/colors.

        Use the exact source copy from which coordinates were obtained. Generic
        rendering needs no layout model instances. The layout convenience path
        checks locators and surface sizes; native flow/sheet/slide maps need a
        separate format renderer and are rejected. Sources and caller inputs are
        unchanged. Returned RGB images are detached; callers can save/display and
        close them. On failure, prepared images are closed and no partial result
        is returned. PDF rendering requires the optional layout-visual extra.
        """
        sizes: dict[int, tuple[float, float]] = {}
        expected_kind = None
        if layout is not None:
            if boxes is not None or labels is not None or colors is not None:
                raise TypeError("pass either layout or boxes/labels/colors, not both")
            boxes, labels, colors, sizes, expected_kind = self._layout_inputs(layout)
        if boxes is None:
            raise TypeError("pass a layout result or boxes")
        boxes = self._validate_inputs(boxes, labels, colors)
        source = self._resolve_source(source)
        if source.path is not None and not S_ISREG(source.path.stat().st_mode):
            raise ValueError("source must be a regular file")
        data = source.read_bytes()
        kind = "page" if b"%PDF-" in data[:1024] else "image"
        if expected_kind is not None and kind != expected_kind:
            raise ValueError("source format does not match the layout surfaces")
        images: dict[int, Image.Image] = {}
        try:
            if kind == "page":
                self._render_pdf(
                    data, source.password, boxes, labels, colors, sizes, images
                )
            else:
                self._render_images(data, boxes, labels, colors, sizes, images)
        except BaseException:
            for image in images.values():
                image.close()
            raise
        return images

    @staticmethod
    def _resolve_source(source: Path | str | bytes | Artifact) -> Artifact:
        """Resolve this renderer's input without depending on another component."""
        if isinstance(source, Artifact):
            return source
        if isinstance(source, bytes):
            return Artifact(data=source)
        if isinstance(source, (Path, str)):
            return Artifact(path=source)
        raise TypeError("source must be a path, bytes, or Artifact")

    @classmethod
    def _layout_inputs(
        cls, layout: "LayoutAnalysisResult"
    ) -> tuple[
        dict[int, list[tuple[float, float, float, float]]],
        dict[int, list[str]],
        dict[int, list[str]],
        dict[int, tuple[float, float]],
        str,
    ]:
        # Layout models are needed only for this optional adapter, not the renderer.
        from ..layout_analysis.models import (
            ImageFrameLocation,
            LayoutAnalysisResult,
            PdfPageLocation,
        )

        if not isinstance(layout, LayoutAnalysisResult):
            raise TypeError("layout must be a LayoutAnalysisResult")
        if not layout.surfaces:
            raise ValueError(
                "layout must contain usable PDF-page or image-frame surfaces"
            )
        kinds = {surface.kind for surface in layout.surfaces}
        if kinds not in ({"page"}, {"image"}):
            raise ValueError("rendering supports PDF-page or image-frame layouts only")
        boxes: dict[int, list[tuple[float, float, float, float]]] = {}
        labels: dict[int, list[str]] = {}
        colors: dict[int, list[str]] = {}
        sizes: dict[int, tuple[float, float]] = {}
        numbers: dict[str, int] = {}
        for surface in layout.surfaces:
            location = surface.location
            assert isinstance(location, (PdfPageLocation, ImageFrameLocation))
            number = (
                location.page_number
                if isinstance(location, PdfPageLocation)
                else location.frame_number
            )
            if number in boxes:
                raise ValueError("layout references the same page/frame more than once")
            assert surface.width is not None and surface.height is not None
            numbers[surface.id_] = number
            sizes[number] = (surface.width, surface.height)
            boxes[number], labels[number], colors[number] = [], [], []
        for region in layout.regions:
            assert region.bbox is not None
            number = numbers[region.surface_id]
            boxes[number].append(region.bbox)
            labels[number].append(f"{region.id} {region.kind}")
            colors[number].append(cls._LAYOUT_COLORS[region.kind])
        return boxes, labels, colors, sizes, next(iter(kinds))

    @staticmethod
    def _validate_inputs(
        boxes: Mapping[int, Sequence[tuple[float, float, float, float]]],
        labels: Mapping[int, Sequence[str]] | None,
        colors: Mapping[int, Sequence[str]] | None,
    ) -> dict[int, tuple[tuple[float, float, float, float], ...]]:
        if not isinstance(boxes, Mapping):
            raise TypeError("boxes must map page/frame numbers to rectangles")
        if not boxes:
            raise ValueError("boxes must select at least one page/frame")
        prepared = {}
        for number, page_boxes in boxes.items():
            if isinstance(number, bool) or not isinstance(number, int):
                raise TypeError("page/frame numbers must be integers")
            if number < 1:
                raise ValueError("page/frame numbers start at one")
            normalized = []
            for box in page_boxes:
                if len(box) != 4:
                    raise ValueError("each box requires left, top, right, bottom")
                if any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) for v in box
                ):
                    raise TypeError("box coordinates must be numbers")
                left, top, right, bottom = map(float, box)
                if (
                    not all(isfinite(v) for v in box)
                    or left < 0
                    or top < 0
                    or right <= left
                    or bottom <= top
                ):
                    raise ValueError(
                        "boxes must be finite, nonnegative rectangles with positive size"
                    )
                normalized.append((left, top, right, bottom))
            prepared[number] = tuple(normalized)
        for name, values in (("labels", labels), ("colors", colors)):
            if values is None:
                continue
            if not isinstance(values, Mapping):
                raise TypeError(f"{name} must map page/frame numbers to strings")
            if any(number not in prepared for number in values):
                raise ValueError(f"{name} refers to an unselected page/frame")
            for number, items in values.items():
                if len(items) != len(prepared[number]):
                    raise ValueError(
                        f"{name} must match the number of boxes on each page/frame"
                    )
                if isinstance(items, str) or any(
                    not isinstance(item, str) for item in items
                ):
                    raise TypeError(f"{name} entries must be sequences of strings")
                if name == "colors":
                    for color in items:
                        ImageColor.getrgb(color)
        return prepared

    @staticmethod
    def _check_geometry(
        number: int,
        width: float,
        height: float,
        boxes: Mapping[int, Sequence[tuple[float, float, float, float]]],
        sizes: Mapping[int, tuple[float, float]],
    ) -> None:
        if not all(isfinite(v) and v > 0 for v in (width, height)):
            raise ValueError("source dimensions must be finite and positive")
        if number in sizes and any(
            not isclose(actual, expected, rel_tol=1e-5, abs_tol=0.01)
            for actual, expected in zip((width, height), sizes[number])
        ):
            raise ValueError("source dimensions do not match the layout surface")
        if any(box[2] > width or box[3] > height for box in boxes[number]):
            raise ValueError("box must fit the page/frame coordinate dimensions")

    def _render_pdf(
        self,
        data: bytes,
        password: str | None,
        boxes: Mapping[int, Sequence[tuple[float, float, float, float]]],
        labels: Mapping[int, Sequence[str]] | None,
        colors: Mapping[int, Sequence[str]] | None,
        sizes: Mapping[int, tuple[float, float]],
        images: dict[int, Image.Image],
    ) -> None:
        try:
            import pypdfium2 as pdfium
        except ImportError as error:
            raise ImportError(
                "PDF region rendering requires: pip install 'raggae[layout-visual]'"
            ) from error
        with pikepdf.Pdf.open(BytesIO(data), password=password or "") as metadata:
            with pdfium.PdfDocument(data, password=password) as document:
                for number in boxes:
                    if number > len(document):
                        raise ValueError(
                            "source does not contain the referenced PDF page"
                        )
                    with closing(document[number - 1]) as page:
                        unit = float(metadata.pages[number - 1].obj.get("/UserUnit", 1))
                        width, height = page.get_size()
                        size = (width * unit, height * unit)
                        self._check_geometry(number, *size, boxes, sizes)
                        scale = self._dpi / 72 * unit
                        if (
                            ceil(width * scale) * ceil(height * scale)
                            > self._max_pixels_per_page
                        ):
                            raise ValueError("PDF render exceeds max_pixels_per_page")
                        with closing(
                            # PDFium accepts fractional scales despite its inferred annotation.
                            page.render(
                                scale=scale,  # pyright: ignore[reportArgumentType]
                                draw_annots=True,
                                may_draw_forms=False,
                            )
                        ) as bitmap:
                            with bitmap.to_pil() as rendered:
                                image = rendered.convert("RGB")
                    images[number] = self._draw_regions(
                        image,
                        size,
                        boxes[number],
                        None if labels is None else labels.get(number),
                        None if colors is None else colors.get(number),
                    )

    def _render_images(
        self,
        data: bytes,
        boxes: Mapping[int, Sequence[tuple[float, float, float, float]]],
        labels: Mapping[int, Sequence[str]] | None,
        colors: Mapping[int, Sequence[str]] | None,
        sizes: Mapping[int, tuple[float, float]],
        images: dict[int, Image.Image],
    ) -> None:
        with BytesIO(data) as stream, Image.open(stream) as document:
            if document.format not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP", "GIF"}:
                raise ValueError("unsupported raster image format")
            for number in boxes:
                try:
                    document.seek(number - 1)
                except EOFError as error:
                    raise ValueError(
                        "source does not contain the referenced image frame"
                    ) from error
                size = document.size
                self._check_geometry(number, *size, boxes, sizes)
                if document.width * document.height > self._max_pixels_per_page:
                    raise ValueError("image exceeds max_pixels_per_page")
                with (
                    document.convert("RGBA") as rgba,
                    Image.new("RGB", document.size, "white") as background,
                ):
                    with rgba.getchannel("A") as alpha:
                        background.paste(rgba, mask=alpha)
                    image = background.copy()
                images[number] = self._draw_regions(
                    image,
                    size,
                    boxes[number],
                    None if labels is None else labels.get(number),
                    None if colors is None else colors.get(number),
                )

    def _draw_regions(
        self,
        image: Image.Image,
        size: tuple[float, float],
        boxes: Sequence[tuple[float, float, float, float]],
        labels: Sequence[str] | None,
        colors: Sequence[str] | None,
    ) -> Image.Image:
        try:
            sx, sy = image.width / size[0], image.height / size[1]
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=12)
            for index, (left, top, right, bottom) in enumerate(boxes):
                box = (
                    min(image.width - 1, left * sx),
                    min(image.height - 1, top * sy),
                    min(image.width - 1, right * sx),
                    min(image.height - 1, bottom * sy),
                )
                color = self._LAYOUT_COLORS["text"] if colors is None else colors[index]
                draw.rectangle(box, outline=color, width=2)
                if self._show_labels:
                    label = f"region-{index + 1}" if labels is None else labels[index]
                    a, b, c, d = draw.textbbox((0, 0), label, font=font)
                    x = max(0.0, min(box[0], image.width - (c - a)))
                    y = max(0.0, box[1] - (d - b) - 4)
                    position = (x - a, y - b)
                    draw.rectangle(
                        draw.textbbox(position, label, font=font), fill="white"
                    )
                    draw.text(position, label, fill=color, font=font)
            return image
        except BaseException:
            image.close()
            raise
