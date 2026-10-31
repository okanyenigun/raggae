"""Keep discovery and independent map-analysis results together."""

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator
from typing import Self

from .models import LayoutAnalysisResult, LayoutFinding, LayoutOutcome
from .map_analysis.reading_order.models import ReadingOrderResult
from .map_analysis.relationships.models import RegionRelationshipResult
from .map_analysis.repeated_margin.models import RepeatedMarginResult


class DocumentLayoutReport(BaseModel):
    """One source map and optional analyses of that same, unchanged map."""

    model_config = ConfigDict(frozen=True)

    analyzer: str | None = Field(
        default=None,
        description="Name of the selected discovery worker, or None if routing could not select one.",
        examples=["layout_analysis_native_pdf", None],
    )
    layout: LayoutAnalysisResult = Field(
        description="The discovery worker's original layout map and coverage findings.",
    )
    reading_order: ReadingOrderResult | None = Field(
        default=None,
        description="Geometric order suggestions, or None when disabled, inapplicable to the map, or discovery was unusable.",
        examples=[None],
    )
    relationships: RegionRelationshipResult | None = Field(
        default=None,
        description="Region containment and caption candidates, or None when disabled or discovery was unusable.",
        examples=[None],
    )
    repeated_margins: RepeatedMarginResult | None = Field(
        default=None,
        description="PDF repeated-margin results, or None when disabled, the map has no PDF pages, or discovery was unusable.",
        examples=[None],
    )

    @computed_field(
        description="Discovery status, downgraded to partial if an executed map analysis is partial or failed. Not-applicable refinements do not downgrade a usable map."
    )
    @property
    def outcome(self) -> LayoutOutcome:
        if self.layout.outcome in ("failed", "not_applicable"):
            return self.layout.outcome
        if self.layout.outcome == "partial" or any(
            result is not None and result.outcome in ("partial", "failed")
            for result in (
                self.reading_order,
                self.relationships,
                self.repeated_margins,
            )
        ):
            return "partial"
        return "success"

    @computed_field(
        description="Findings from discovery and executed analyses, with identical inherited findings included only once."
    )
    @property
    def findings(self) -> tuple[LayoutFinding, ...]:
        return tuple(
            dict.fromkeys(
                finding
                for result in (
                    self.layout,
                    self.reading_order,
                    self.relationships,
                    self.repeated_margins,
                )
                if result is not None
                for finding in result.findings
            )
        )

    @model_validator(mode="after")
    def _same_map(self) -> Self:
        results = (self.reading_order, self.relationships, self.repeated_margins)
        if self.layout.outcome in ("failed", "not_applicable") and any(
            result is not None for result in results
        ):
            raise ValueError("map analyses require usable discovery")
        references: set[str] = set()
        if self.reading_order is not None:
            references.update(self.reading_order.unresolved_region_ids)
            for edge in self.reading_order.edges:
                references.update((edge.before_region_id, edge.after_region_id))
        if self.relationships is not None:
            references.update(self.relationships.unresolved_region_ids)
            for link in self.relationships.relationships:
                references.update((link.source_region_id, link.target_region_id))
        if self.repeated_margins is not None:
            for group in self.repeated_margins.groups:
                references.update(group.region_ids)
        if references - {region.id for region in self.layout.regions}:
            raise ValueError("map analysis references region IDs absent from discovery")
        return self
