"""The pages of a source, drawn on demand as pictures; shared by OCR and image extraction."""

from typing import Any
from PIL import Image
from .layout_analysis.models import PdfPageLocation


class Pages:
    count: int

    def locate(self, location: Any) -> int | None: ...

    def render(self, index: int) -> tuple[Image.Image, tuple[float, float]]: ...

    def close(self) -> None: ...


class PdfPages(Pages):
    """PDF pages rendered on demand; size is the displayed page in points."""

    def __init__(self, document: Any, units: list[float], dpi: float) -> None:
        self._document, self._units, self._dpi = document, units, dpi
        self.count = len(document)

    def locate(self, location: Any) -> int | None:
        return (
            location.page_number - 1 if isinstance(location, PdfPageLocation) else None
        )

    def render(self, index: int) -> tuple[Image.Image, tuple[float, float]]:
        import pypdfium2 as pdfium

        unit = self._units[index]
        page = self._document[index]
        try:
            width, height = page.get_size()
            bitmap = page.render(
                scale=self._dpi / 72 * unit,  # pyright: ignore[reportArgumentType]
                draw_annots=False,
                may_draw_forms=False,
            )
            try:
                with bitmap.to_pil() as rendered:
                    image = rendered.convert("RGB")
            finally:
                bitmap.close()
        except pdfium.PdfiumError as error:
            raise RuntimeError("page could not be rendered") from error
        finally:
            page.close()
        return image, (width * unit, height * unit)

    def close(self) -> None:
        self._document.close()
