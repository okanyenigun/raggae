"""Read stored HTML text by element, without running scripts or applying CSS."""

import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import (
    as_artifact,
    failure,
    nothing_selected,
    read_source_text,
    require_encoding,
    select_regions,
)
from ...layout_analysis.models import (
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...normalization.artifacts import Artifact
from ...shared.html_text import (
    BLOCK_LEVEL,
    HEADINGS,
    SKIPPED,
    TEXT_BLOCKS,
    UNSUPPORTED,
    TextLines,
    alt,
    element_text,
    is_tag,
    is_text,
    lineage,
    resolve,
)
from ...normalization.models import TextEncoding


class HtmlTextReader:
    """Return plain-text blocks from stored HTML elements.

    Without a layout, each heading, paragraph, list item, quote, preformatted
    block, caption, or table becomes one block, and loose text between them
    forms its own blocks. With a layout from ``HtmlLayoutAnalyzer``, each
    selected region becomes one block; text inside a nested selected region
    belongs only to that inner region. Tables put one row per line with cells
    joined by `` | ``. Image alt text is read as text. Scripts, styles,
    comments, templates, ``noscript``, and ``<head>`` (including ``<title>``)
    are never read; elements with the ``hidden`` attribute are skipped with a
    finding. Nothing is fetched or executed and CSS is not applied.
    """

    _OWN_BLOCK = HEADINGS | TEXT_BLOCKS | {"table"}
    # Notes describe declared exclusions; they never make a read incomplete.
    _INFORMATIONAL = frozenset(
        {
            FindingCode.EMPTY,
            FindingCode.HIDDEN_SKIPPED,
            FindingCode.UNSUPPORTED_CONTENT,
        }
    )

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_html"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        artifact = as_artifact(source)
        selected = select_regions(layout, region_ids)
        if selected == ():
            return nothing_selected(self.name)
        text = read_source_text(artifact, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise ImportError(
                "HTML text reading requires: pip install 'raggae[text-html]'"
            ) from error
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                document = BeautifulSoup(text, "html.parser")
            if selected is None:
                return self._read_document(document)
            return self._read_regions(document, selected)
        except RecursionError:
            return failure(
                self.name,
                FindingCode.STRUCTURE_UNSUPPORTED,
                "HTML nesting is too deep to read within Python's recursion limit.",
            )
        except (ValueError, AssertionError):
            return failure(
                self.name,
                FindingCode.PARSE_FAILED,
                "Source could not be parsed as HTML.",
            )

    def _read_document(self, document: Any) -> TextReadResult:
        notes: set[FindingCode] = set()
        blocks: list[TextBlock] = []
        run = TextLines()

        def flush() -> None:
            nonlocal run
            if (content := run.result()) is not None:
                blocks.append(TextBlock(text=content, page_index=0))
            run = TextLines()

        def walk(node: Any) -> None:
            for child in node.children:
                if not is_tag(child):
                    if is_text(child):
                        run.inline(str(child))
                    continue
                name = child.name
                if name in SKIPPED:
                    continue
                if child.has_attr("hidden"):
                    notes.add(FindingCode.HIDDEN_SKIPPED)
                    continue
                if name in UNSUPPORTED:
                    notes.add(FindingCode.UNSUPPORTED_CONTENT)
                    continue
                if name in HtmlTextReader._OWN_BLOCK:
                    flush()
                    if (content := element_text(child, frozenset(), notes)) is not None:
                        blocks.append(TextBlock(text=content, page_index=0))
                elif name == "br":
                    run.hard_break()
                elif name == "img":
                    run.inline(alt(child))
                elif name in BLOCK_LEVEL:
                    flush()
                    walk(child)
                    flush()
                else:
                    walk(child)

        walk(document)
        flush()
        findings = [HtmlTextReader._note(code) for code in sorted(notes)]
        if not blocks:
            findings.append(
                HtmlTextReader._finding(
                    FindingCode.EMPTY, "Source contains no readable HTML text."
                )
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _read_regions(
        self, document: Any, selected: tuple[LayoutRegion, ...]
    ) -> TextReadResult:
        findings: list[TextReadFinding] = []
        resolved: list[tuple[LayoutRegion, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, HtmlElementLocation):
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This region has no HTML element location.",
                        region,
                    )
                )
                continue
            element = resolve(document, location.child_indexes)
            if element is None or element.name != location.tag:
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's element location does not match this source.",
                        region,
                    )
                )
            elif element.name in UNSUPPORTED or any(
                ancestor.name in SKIPPED for ancestor in lineage(element)
            ):
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This element's content is outside what HtmlTextReader reads.",
                        region,
                    )
                )
            else:
                resolved.append((region, element))

        counts: dict[int, int] = {}
        for _, element in resolved:
            counts[id(element)] = counts.get(id(element), 0) + 1
        owners = frozenset(id(element) for _, element in resolved)
        blocks: list[TextBlock] = []
        completed = 0
        for region, element in resolved:
            if counts[id(element)] > 1:
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Another selected region locates the same element; neither was read.",
                        region,
                    )
                )
                continue
            completed += 1
            if any(ancestor.has_attr("hidden") for ancestor in lineage(element)):
                findings.append(
                    HtmlTextReader._note(FindingCode.HIDDEN_SKIPPED, region)
                )
                continue
            notes: set[FindingCode] = set()
            content = element_text(element, owners - {id(element)}, notes)
            findings.extend(
                HtmlTextReader._note(code, region) for code in sorted(notes)
            )
            if content is None:
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.EMPTY,
                        "Region contains no readable HTML text.",
                        region,
                    )
                )
            else:
                blocks.append(
                    TextBlock(text=content, page_index=0, region_id=region.id)
                )
        incomplete = any(f.code not in HtmlTextReader._INFORMATIONAL for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    @staticmethod
    def _finding(
        code: FindingCode, message: str, region: LayoutRegion | None = None
    ) -> TextReadFinding:
        return TextReadFinding(
            code=code,
            message=message,
            region_id=None if region is None else region.id,
            page_index=0,
        )

    @staticmethod
    def _note(code: FindingCode, region: LayoutRegion | None = None) -> TextReadFinding:
        messages = {
            FindingCode.HIDDEN_SKIPPED: "Elements marked hidden were not read.",
            FindingCode.UNSUPPORTED_CONTENT: (
                "Embedded, interactive, or vector elements were not read."
            ),
        }
        return HtmlTextReader._finding(code, messages[code], region)
