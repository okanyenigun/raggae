"""Suggested geometric precedence, separate from extracted-content ordering."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal, Self
from ...models import LayoutFinding, LayoutOutcome


class ReadingOrderEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    before_region_id: str = Field(min_length=1)
    after_region_id: str = Field(min_length=1)
    basis: Literal["vertical_gap", "horizontal_gap", "page_order"]

    @model_validator(mode="after")
    def _different_regions(self) -> Self:
        if self.before_region_id == self.after_region_id:
            raise ValueError("an ordering edge cannot reference itself")
        return self


class ReadingOrderResult(BaseModel):
    """Edges are suggestions; success means geometric processing completed."""

    model_config = ConfigDict(frozen=True)

    outcome: LayoutOutcome
    edges: tuple[ReadingOrderEdge, ...] = ()
    unresolved_region_ids: tuple[str, ...] = ()
    findings: tuple[LayoutFinding, ...] = ()

    @model_validator(mode="after")
    def _acyclic(self) -> Self:
        successors: dict[str, set[str]] = {}
        incoming: dict[str, int] = {}
        for edge in self.edges:
            before, after = edge.before_region_id, edge.after_region_id
            following = successors.setdefault(before, set())
            if after in following:
                raise ValueError("ordering edges must not repeat")
            following.add(after)
            incoming.setdefault(before, 0)
            incoming[after] = incoming.get(after, 0) + 1
        ready = [identifier for identifier, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            identifier = ready.pop()
            visited += 1
            for following in successors.get(identifier, ()):
                incoming[following] -= 1
                if incoming[following] == 0:
                    ready.append(following)
        if visited != len(incoming):
            raise ValueError("ordering edges must not contain a cycle")
        if len(set(self.unresolved_region_ids)) != len(self.unresolved_region_ids):
            raise ValueError("unresolved region IDs must not repeat")
        if self.unresolved_region_ids and self.outcome != "partial":
            raise ValueError("unresolved regions require a partial result")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        if self.outcome in ("failed", "not_applicable") and self.edges:
            raise ValueError(
                "failed/not-applicable results cannot contain ordering edges"
            )
        return self
