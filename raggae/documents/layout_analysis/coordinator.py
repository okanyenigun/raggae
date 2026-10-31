"""Route discovery, then apply independent workers to the original layout map."""

from pathlib import Path
from typing import Literal
from .base import LayoutAnalyzer
from .models import LayoutAnalysisResult, LayoutFinding, PdfPageLocation
from .report import DocumentLayoutReport
from .utils import resolve_source
from .region_discovery.pdf import NativePdfLayoutAnalyzer
from .region_discovery.docx import DocxLayoutAnalyzer
from .region_discovery.pptx import PptxLayoutAnalyzer
from .region_discovery.xlsx import XlsxLayoutAnalyzer
from .region_discovery.html import HtmlLayoutAnalyzer
from .region_discovery.visual import LayoutDetectionService, VisualLayoutAnalyzer
from .map_analysis.reading_order import (
    GeometricReadingOrderResolver,
    ReadingOrderResolver,
)
from .map_analysis.relationships import (
    RegionRelationshipDetector,
    RuleBasedRegionRelationshipDetector,
)
from .map_analysis.repeated_margin import (
    PdfRepeatedMarginDetector,
    RepeatedMarginDetector,
)
from ..normalization.artifacts import Artifact


class DocumentLayoutAnalyzer:
    """Coordinate existing workers without changing their interfaces or outputs.

    An explicit analyzer overrides routing. Otherwise, paths route by extension;
    bytes and extensionless paths require source_format. Image routing requires
    an explicitly configured visual service. No content sniffing, automatic
    model loading, fallback discovery or semantic ordering takes place here.
    """

    def __init__(
        self,
        *,
        analyzer: LayoutAnalyzer | None = None,
        visual_service: LayoutDetectionService | None = None,
        reading_order_resolver: ReadingOrderResolver | None = None,
        relationship_detector: RegionRelationshipDetector | None = None,
        repeated_margin_detector: RepeatedMarginDetector | None = None,
    ) -> None:
        if analyzer is not None and visual_service is not None:
            raise ValueError(
                "supply an analyzer override or a visual_service for routing, not both"
            )
        self._analyzer = analyzer
        self._visual_service = visual_service
        self._reading_order_resolver = (
            GeometricReadingOrderResolver()
            if reading_order_resolver is None
            else reading_order_resolver
        )
        self._relationship_detector = (
            RuleBasedRegionRelationshipDetector()
            if relationship_detector is None
            else relationship_detector
        )
        self._repeated_margin_detector = (
            PdfRepeatedMarginDetector()
            if repeated_margin_detector is None
            else repeated_margin_detector
        )

    def analyze(
        self,
        source: Path | str | bytes | Artifact,
        *,
        source_format: str | None = None,
        direction: Literal["ltr", "rtl"] = "ltr",
        reading_order: bool = True,
        relationships: bool = True,
        repeated_margins: bool = True,
    ) -> DocumentLayoutReport:
        """Discover once and return each applicable analysis alongside that map.

        Disable any map-analysis job with its boolean flag. Results marked failed
        or not_applicable by discovery stop the flow. A failed refinement keeps
        the usable map and does not stop the other independent analyses. Worker
        configuration, missing dependencies and unexpected exceptions propagate.
        """
        source = resolve_source(source)
        if source_format is not None and not isinstance(source_format, str):
            raise TypeError("source_format must be a string or None")
        if direction not in ("ltr", "rtl"):
            raise ValueError("direction must be 'ltr' or 'rtl'")
        for flag in (reading_order, relationships, repeated_margins):
            if not isinstance(flag, bool):
                raise TypeError("map-analysis flags must be booleans")

        analyzer = self._analyzer
        if analyzer is None:
            format_ = source_format
            if format_ is None and source.path is not None:
                format_ = source.path.suffix
            if format_ is None or not format_.strip().removeprefix("."):
                raise ValueError(
                    "source_format is required for bytes or a source without an extension"
                )
            format_ = format_.strip().removeprefix(".").casefold()
            native: dict[str, type[LayoutAnalyzer]] = {
                "pdf": NativePdfLayoutAnalyzer,
                "docx": DocxLayoutAnalyzer,
                "pptx": PptxLayoutAnalyzer,
                "xlsx": XlsxLayoutAnalyzer,
                "html": HtmlLayoutAnalyzer,
            }
            if format_ in native:
                analyzer = native[format_]()
            elif format_ in {"png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp", "gif"}:
                if self._visual_service is None:
                    return DocumentLayoutReport(
                        layout=LayoutAnalysisResult(
                            outcome="failed",
                            findings=(
                                LayoutFinding(
                                    code="layout.coordinator.visual_service_required",
                                    message="Image discovery requires an explicitly configured visual_service or analyzer.",
                                ),
                            ),
                        )
                    )
                analyzer = VisualLayoutAnalyzer(service=self._visual_service)
            else:
                return DocumentLayoutReport(
                    layout=LayoutAnalysisResult(
                        outcome="not_applicable",
                        findings=(
                            LayoutFinding(
                                code="layout.coordinator.unsupported_format",
                                message="No default layout discovery worker supports this source format.",
                            ),
                        ),
                    )
                )

        layout = analyzer.analyze(source)
        if layout.outcome in ("failed", "not_applicable"):
            return DocumentLayoutReport(analyzer=analyzer.name, layout=layout)

        order_result = None
        if reading_order and any(
            surface.width is not None and surface.height is not None
            for surface in layout.surfaces
        ):
            order_result = self._reading_order_resolver.resolve_order(
                layout, direction=direction
            )

        relationship_result = None
        if relationships:
            relationship_result = self._relationship_detector.detect(layout)

        margin_result = None
        if repeated_margins and any(
            surface.kind == "page" and isinstance(surface.location, PdfPageLocation)
            for surface in layout.surfaces
        ):
            margin_result = self._repeated_margin_detector.detect(layout, source)

        return DocumentLayoutReport(
            analyzer=analyzer.name,
            layout=layout,
            reading_order=order_result,
            relationships=relationship_result,
            repeated_margins=margin_result,
        )
