"""Conservative text/page-number signatures and position-aware grouping."""

from dataclasses import dataclass
from math import ceil
import re
from typing import Literal
from unicodedata import normalize

from .models import RepeatedMarginGroup


@dataclass(frozen=True, slots=True)
class Candidate:
    region_id: str
    page_number: int
    kind: Literal["header", "footer"]
    key: str
    basis: Literal["repeated_text", "page_number"]
    x: float
    y: float


def signature(text: str, page_number: int) -> tuple[str, Literal["repeated_text", "page_number"]]:
    """Only normalize numeric page labels agreeing with the PDF page index."""
    text = " ".join(normalize("NFKC", text).casefold().split())
    bare = re.fullmatch(r"(?:[-–—]\s*)?(\d+)(?:\s*/\s*\d+)?(?:\s*[-–—])?", text)
    if bare is not None and int(bare[1]) == page_number:
        return text[:bare.start(1)] + "#" + text[bare.end(1):], "page_number"
    label = re.search(r"\bpage\s+(\d+)(?:\s*(?:of|/)\s*\d+)?\b", text)
    if label is not None and int(label[1]) == page_number:
        return text[:label.start(1)] + "#" + text[label.end(1):], "page_number"
    return text, "repeated_text"


def group_candidates(candidates: list[Candidate], *, page_count: int, minimum_pages: int,
                     minimum_page_fraction: float, position_tolerance: float) -> tuple[tuple[RepeatedMarginGroup, ...], bool]:
    buckets: dict[tuple[str, str, str], list[list[Candidate]]] = {}
    for candidate in sorted(candidates, key=lambda item: (item.page_number, item.y, item.x, item.region_id)):
        clusters = buckets.setdefault((candidate.kind, candidate.key, candidate.basis), [])
        for cluster in clusters:
            reference = cluster[0]
            if abs(candidate.x - reference.x) <= position_tolerance and abs(candidate.y - reference.y) <= position_tolerance:
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    required = max(minimum_pages, ceil(page_count * minimum_page_fraction))
    groups = []
    ambiguous = False
    for clusters in buckets.values():
        for cluster in clusters:
            pages = {candidate.page_number for candidate in cluster}
            if len(pages) != len(cluster):
                ambiguous = True
                continue
            if len(pages) >= required:
                groups.append(RepeatedMarginGroup(kind=cluster[0].kind, basis=cluster[0].basis,
                                                  region_ids=tuple(candidate.region_id for candidate in cluster)))
    return tuple(groups), ambiguous
