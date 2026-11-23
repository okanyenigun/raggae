"""Pictures on Excel worksheets, with openpyxl."""

import warnings
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
    empty_region,
    failure,
    finish,
    nothing_selected,
    overlap,
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
    XlsxDrawingLocation,
    XlsxRangeLocation,
)
from ...layout_analysis.utils import cell_bounds
from ...normalization.artifacts import Artifact
from ...sources import as_artifact


class XlsxImageExtractor:
    """Return the pictures placed on a workbook's worksheets, with openpyxl.

    Without a layout: every picture openpyxl loads for each worksheet, in its
    order, with ``page_index`` the sheet's zero-based position in the workbook
    (chart sheets included). ``alt_text`` is the picture's stored description.

    Each picture is the file stored in the workbook: PNG, JPEG, TIFF, GIF, and
    WebP byte for byte (``original``), other raster formats as new files
    (``reencoded``). openpyxl drops some pictures while loading, announcing
    each only with a warning; those warnings become findings
    (``image_unreadable``, ``unsupported_image_type``, or ``backend_coverage``
    when a whole drawing was dropped), so a lost picture is never silent.

    Not extracted, each with an informational ``unsupported_content``: charts
    and chart sheets (per sheet), and pictures placed inside cells (once).
    Header and footer pictures are not read.

    With a layout: an image region (a drawing location) gives that picture; a
    cell-range region requested by ID gives the pictures anchored in it. Without
    ``region_ids`` only image regions are used. A chart location gets
    ``selection_unsupported``; a region without a picture gets ``empty``. A
    drawing region owns its picture before any range; of ranges, the innermost
    owns it, and ranges claiming it without nesting are both refused
    (``selection_overlap``).
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.NOTHING_SELECTED,
            ImageFindingCode.UNSUPPORTED_CONTENT,
        }
    )

    @property
    def name(self) -> str:
        return "image_extractor_xlsx"

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
            from openpyxl import load_workbook
        except ImportError as error:
            raise ImportError(
                "Excel image extraction requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                workbook = load_workbook(BytesIO(data))
        except (BadZipFile, KeyError, ValueError, TypeError, AttributeError, OSError):
            return failure(
                self.name,
                ImageFindingCode.DECODE_FAILED,
                "Source could not be opened as a workbook.",
            )
        findings = XlsxImageExtractor._load_findings(caught)
        sheets = [workbook[name] for name in workbook.sheetnames]
        if any(name.startswith("xl/richData/") for name in names):
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.UNSUPPORTED_CONTENT,
                    message="Pictures placed inside cells were not extracted.",
                )
            )
        if selected is None:
            return self._standalone(sheets, findings)
        return self._regions(sheets, selected, findings)

    def _standalone(
        self, sheets: list[Any], findings: list[ImageExtractFinding]
    ) -> ImageExtractResult:
        images: list[ExtractedImage] = []
        found = 0
        for index, sheet in enumerate(sheets):
            pictures = XlsxImageExtractor._pictures(sheet)
            found += len(pictures)
            for picture in pictures:
                XlsxImageExtractor._extract(picture, index, None, images, findings)
            if XlsxImageExtractor._has_charts(sheet):
                findings.append(XlsxImageExtractor._charts_finding(index))
        if not found:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY,
                    message="No pictures on the worksheets.",
                )
            )
        return finish(self.name, images, findings, 1, self._INFORMATIONAL)

    def _regions(
        self,
        sheets: list[Any],
        selected: tuple[LayoutRegion, ...],
        findings: list[ImageExtractFinding],
    ) -> ImageExtractResult:
        by_name = {sheet.title: (index, sheet) for index, sheet in enumerate(sheets)}
        # Per region: its sheet, and the pictures it claims (a drawing, or a range).
        claims: list[
            tuple[LayoutRegion, int, list[int], tuple[int, int, int, int] | None]
        ] = []
        for region in selected:
            location = region.location
            if not isinstance(location, (XlsxDrawingLocation, XlsxRangeLocation)):
                findings.append(
                    unsupported_selection(
                        region.id, "Selection is not a worksheet location."
                    )
                )
                continue
            if location.sheet_name not in by_name:
                findings.append(unresolved(region.id, None))
                continue
            index, sheet = by_name[location.sheet_name]
            pictures = XlsxImageExtractor._pictures(sheet)
            if isinstance(location, XlsxDrawingLocation):
                if location.drawing_kind == "chart":
                    findings.append(
                        unsupported_selection(
                            region.id, "Charts are not stored pictures."
                        )
                    )
                    continue
                if location.drawing_index >= len(pictures):
                    findings.append(unresolved(region.id, index))
                    continue
                claims.append((region, index, [location.drawing_index], None))
            else:
                box = XlsxImageExtractor._range(location.cell_range)
                inside = [
                    number
                    for number, picture in enumerate(pictures)
                    if (anchor := XlsxImageExtractor._anchor(picture)) is not None
                    and box[0] <= anchor[0] <= box[2]
                    and box[1] <= anchor[1] <= box[3]
                ]
                claims.append((region, index, inside, box))

        owned, refused = XlsxImageExtractor._owners(claims)
        images: list[ExtractedImage] = []
        completed = 0
        for number, (region, index, pictures, _) in enumerate(claims):
            if number in refused:
                findings.append(overlap(region.id, index))
                continue
            completed += 1
            if not pictures:
                findings.append(empty_region(region.id, index))
            sheet_pictures = XlsxImageExtractor._pictures(sheets[index])
            for picture in owned[number]:
                XlsxImageExtractor._extract(
                    sheet_pictures[picture], index, region.id, images, findings
                )
        return finish(self.name, images, findings, completed, self._INFORMATIONAL)

    @staticmethod
    def _pictures(sheet: Any) -> list[Any]:
        # openpyxl's loaded pictures; a private attribute, pinned by a test.
        return list(getattr(sheet, "_images", ()))

    @staticmethod
    def _has_charts(sheet: Any) -> bool:
        return bool(getattr(sheet, "_charts", ())) or not hasattr(sheet, "_images")

    @staticmethod
    def _charts_finding(index: int) -> ImageExtractFinding:
        return ImageExtractFinding(
            code=ImageFindingCode.UNSUPPORTED_CONTENT,
            message="Charts on this sheet are not stored pictures and were not extracted.",
            page_index=index,
        )

    @staticmethod
    def _extract(
        picture: Any,
        index: int,
        region_id: str | None,
        images: list[ExtractedImage],
        findings: list[ImageExtractFinding],
    ) -> None:
        reference = picture.ref
        data = reference.getvalue() if isinstance(reference, BytesIO) else None
        if data is None:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.IMAGE_UNREADABLE,
                    message="One picture's stored file was not available.",
                    page_index=index,
                    region_id=region_id,
                )
            )
            return
        extracted = stored_picture(
            data,
            page_index=index,
            region_id=region_id,
            alt_text=XlsxImageExtractor._description(picture),
        )
        if isinstance(extracted, ExtractedImage):
            images.append(extracted)
        else:
            findings.append(extracted)

    @staticmethod
    def _description(picture: Any) -> str | None:
        properties = getattr(
            getattr(getattr(picture.anchor, "pic", None), "nvPicPr", None),
            "cNvPr",
            None,
        )
        return getattr(properties, "descr", None)

    @staticmethod
    def _anchor(picture: Any) -> tuple[int, int] | None:
        """The one-based (column, row) of the cell a picture is anchored to."""
        marker = getattr(picture.anchor, "_from", None)
        return None if marker is None else (marker.col + 1, marker.row + 1)

    @staticmethod
    def _range(cell_range: str) -> tuple[int, int, int, int]:
        """(left column, top row, right column, bottom row), one-based."""
        first, _, last = cell_range.partition(":")
        left, top = cell_bounds(first)
        right, bottom = cell_bounds(last or first)
        return left, top, right, bottom

    @staticmethod
    def _owners(
        claims: list[
            tuple[LayoutRegion, int, list[int], tuple[int, int, int, int] | None]
        ],
    ) -> tuple[list[list[int]], set[int]]:
        """Drawing regions own their picture first; of ranges, the innermost owns it.

        Two drawing regions on the same picture, or ranges that claim one picture
        without one containing the other, are refused.
        """
        by_picture: dict[tuple[int, int], list[int]] = {}
        for number, (_, sheet, pictures, _) in enumerate(claims):
            for picture in pictures:
                by_picture.setdefault((sheet, picture), []).append(number)
        owner: dict[tuple[int, int], int] = {}
        refused: set[int] = set()
        for key, numbers in by_picture.items():
            drawings = [n for n in numbers if claims[n][3] is None]
            if len(drawings) > 1:
                refused.update(drawings)
                continue
            if drawings:
                owner[key] = drawings[0]
                continue
            innermost = [
                n
                for n in numbers
                if all(
                    XlsxImageExtractor._contains(claims[m][3], claims[n][3])
                    for m in numbers
                )
            ]
            if len(innermost) == 1:
                owner[key] = innermost[0]
            else:
                refused.update(numbers)
        owned = [
            [p for p in pictures if owner.get((sheet, p)) == number]
            for number, (_, sheet, pictures, _) in enumerate(claims)
        ]
        return owned, refused

    @staticmethod
    def _contains(outer: Any, inner: Any) -> bool:
        return (
            outer[0] <= inner[0]
            and outer[1] <= inner[1]
            and outer[2] >= inner[2]
            and outer[3] >= inner[3]
        )

    @staticmethod
    def _load_findings(
        caught: list[warnings.WarningMessage],
    ) -> list[ImageExtractFinding]:
        """openpyxl's warnings about dropped pictures, as findings without its wording."""
        findings: list[ImageExtractFinding] = []
        for warning in caught:
            if not warning.filename.replace("\\", "/").endswith(
                "openpyxl/reader/drawings.py"
            ):
                continue
            text = str(warning.message)
            if text.startswith("Unable to read chart"):
                continue  # a chart is not a picture; other warnings mention charts too
            if "format is not supported" in text:
                code = ImageFindingCode.UNSUPPORTED_IMAGE_TYPE
                message = "openpyxl dropped a picture whose format it cannot keep (WMF or EMF)."
            elif "cannot be read" in text:
                code = ImageFindingCode.IMAGE_UNREADABLE
                message = "openpyxl dropped a picture it could not read."
            else:
                code = ImageFindingCode.BACKEND_COVERAGE
                message = "openpyxl dropped a drawing it could not read; its pictures are missing."
            findings.append(ImageExtractFinding(code=code, message=message))
        return findings
