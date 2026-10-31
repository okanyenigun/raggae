"""Candidate recurring page furniture, referencing an existing layout map."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...models import LayoutFinding, LayoutOutcome


class RepeatedMarginGroup(BaseModel):
    """A proposed header/footer group, not an instruction to delete contents."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["header", "footer"]
    basis: Literal["repeated_text", "page_number"]
    region_ids: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _distinct_regions(self) -> Self:
        if any(not identifier for identifier in self.region_ids) or len(set(self.region_ids)) != len(self.region_ids):
            raise ValueError("group region IDs must be nonempty and distinct")
        return self


class RepeatedMarginResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: LayoutOutcome
    groups: tuple[RepeatedMarginGroup, ...] = ()
    findings: tuple[LayoutFinding, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        identifiers = [identifier for group in self.groups for identifier in group.region_ids]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("a region cannot belong to more than one margin group")
        if self.outcome in ("failed", "not_applicable") and self.groups:
            raise ValueError("failed/not-applicable results cannot contain groups")
        if self.outcome != "success" and not self.findings:
            raise ValueError("incomplete results require an explanation")
        return self
