"""Evidence-labelled links between existing region IDs, not merged contents."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal, Self
from ...models import LayoutFinding, LayoutOutcome


class RegionRelationship(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["contains", "caption_candidate"]
    source_region_id: str = Field(min_length=1)
    target_region_id: str = Field(min_length=1)
    basis: Literal[
        "bbox_containment",
        "native_ancestry",
        "cell_range_containment",
        "proximity",
        "shared_parent",
    ]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.source_region_id == self.target_region_id:
            raise ValueError("a relationship cannot reference itself")
        caption_basis = self.basis in ("proximity", "shared_parent")
        if (self.kind == "caption_candidate") != caption_basis:
            raise ValueError("relationship kind and evidence basis must agree")
        return self


class RegionRelationshipResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: LayoutOutcome
    relationships: tuple[RegionRelationship, ...] = ()
    unresolved_region_ids: tuple[str, ...] = ()
    findings: tuple[LayoutFinding, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [
            (link.kind, link.source_region_id, link.target_region_id)
            for link in self.relationships
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("relationships must not repeat")
        if len(set(self.unresolved_region_ids)) != len(
            self.unresolved_region_ids
        ) or any(not identifier for identifier in self.unresolved_region_ids):
            raise ValueError("unresolved region IDs must be nonempty and distinct")
        if self.unresolved_region_ids and self.outcome != "partial":
            raise ValueError("unresolved regions require a partial result")
        if self.outcome in ("failed", "not_applicable") and self.relationships:
            raise ValueError(
                "failed/not-applicable results cannot contain relationships"
            )
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
