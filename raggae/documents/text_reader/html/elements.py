"""Read stored HTML text by element, without running scripts or applying CSS."""

import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from .text_lines import TextLines
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

    # Element groups follow the HTML layout analyzer so both stages agree.
    _HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _TEXT_BLOCKS = frozenset({"p", "pre", "li", "dt", "dd", "blockquote", "figcaption"})
    _UNSUPPORTED = frozenset(
        {
            "svg",
            "math",
            "iframe",
            "object",
            "embed",
            "canvas",
            "video",
            "audio",
            "input",
            "button",
            "select",
            "textarea",
        }
    )
    # Never text: executable, styling, inert, or document-metadata elements.
    _SKIPPED = frozenset({"script", "style", "template", "noscript", "head", "title"})
    _BLOCK_LEVEL = (
        _HEADINGS
        | _TEXT_BLOCKS
        | frozenset(
            {
                "address",
                "article",
                "aside",
                "body",
                "caption",
                "center",
                "details",
                "dialog",
                "dir",
                "div",
                "dl",
                "fieldset",
                "figure",
                "footer",
                "form",
                "header",
                "hgroup",
                "hr",
                "html",
                "legend",
                "main",
                "menu",
                "nav",
                "ol",
                "search",
                "section",
                "summary",
                "table",
                "tbody",
                "td",
                "tfoot",
                "th",
                "thead",
                "tr",
                "ul",
            }
        )
    )
    _OWN_BLOCK = _HEADINGS | _TEXT_BLOCKS | {"table"}
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
                if not HtmlTextReader._is_tag(child):
                    if HtmlTextReader._is_text(child):
                        run.inline(str(child))
                    continue
                name = child.name
                if name in HtmlTextReader._SKIPPED:
                    continue
                if child.has_attr("hidden"):
                    notes.add(FindingCode.HIDDEN_SKIPPED)
                    continue
                if name in HtmlTextReader._UNSUPPORTED:
                    notes.add(FindingCode.UNSUPPORTED_CONTENT)
                    continue
                if name in HtmlTextReader._OWN_BLOCK:
                    flush()
                    if (
                        content := HtmlTextReader._element_text(
                            child, frozenset(), notes
                        )
                    ) is not None:
                        blocks.append(TextBlock(text=content, page_index=0))
                elif name == "br":
                    run.hard_break()
                elif name == "img":
                    run.inline(HtmlTextReader._alt(child))
                elif name in HtmlTextReader._BLOCK_LEVEL:
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
            element = HtmlTextReader._resolve(document, location.child_indexes)
            if element is None or element.name != location.tag:
                findings.append(
                    HtmlTextReader._finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's element location does not match this source.",
                        region,
                    )
                )
            elif element.name in HtmlTextReader._UNSUPPORTED or any(
                ancestor.name in HtmlTextReader._SKIPPED
                for ancestor in HtmlTextReader._lineage(element)
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
            if any(
                ancestor.has_attr("hidden")
                for ancestor in HtmlTextReader._lineage(element)
            ):
                findings.append(
                    HtmlTextReader._note(FindingCode.HIDDEN_SKIPPED, region)
                )
                continue
            notes: set[FindingCode] = set()
            content = HtmlTextReader._element_text(
                element, owners - {id(element)}, notes
            )
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
    def _element_text(
        element: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        """Text of one element; descendants in ``excluded`` belong to other blocks."""
        name = element.name
        if name == "img":
            alt = TextLines.collapse(HtmlTextReader._alt(element)).strip(" ")
            return alt if alt.strip() else None
        if name == "table":
            return HtmlTextReader._table_text(element, excluded, notes)
        if name == "pre":
            return HtmlTextReader._pre_text(element, excluded, notes)
        text = TextLines()
        HtmlTextReader._render(element, text, excluded, notes)
        return text.result()

    @staticmethod
    def _render(
        node: Any,
        text: TextLines,
        excluded: frozenset[int],
        notes: set[FindingCode],
    ) -> None:
        for child in node.children:
            if not HtmlTextReader._is_tag(child):
                if HtmlTextReader._is_text(child):
                    text.inline(str(child))
                continue
            name = child.name
            if id(child) in excluded:
                if name in HtmlTextReader._BLOCK_LEVEL:
                    text.soft_break()
                continue
            if name in HtmlTextReader._SKIPPED:
                continue
            if child.has_attr("hidden"):
                notes.add(FindingCode.HIDDEN_SKIPPED)
            elif name in HtmlTextReader._UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)
            elif name == "br":
                text.hard_break()
            elif name == "img":
                text.inline(HtmlTextReader._alt(child))
            elif name in ("pre", "table"):
                text.verbatim(HtmlTextReader._element_text(child, excluded, notes))
            elif name in HtmlTextReader._BLOCK_LEVEL:
                text.soft_break()
                HtmlTextReader._render(child, text, excluded, notes)
                text.soft_break()
            else:
                HtmlTextReader._render(child, text, excluded, notes)

    @staticmethod
    def _pre_text(
        element: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        pieces: list[str] = []

        def collect(node: Any) -> None:
            for child in node.children:
                if not HtmlTextReader._is_tag(child):
                    if HtmlTextReader._is_text(child):
                        pieces.append(str(child))
                    continue
                if id(child) in excluded or child.name in HtmlTextReader._SKIPPED:
                    continue
                if child.has_attr("hidden"):
                    notes.add(FindingCode.HIDDEN_SKIPPED)
                elif child.name in HtmlTextReader._UNSUPPORTED:
                    notes.add(FindingCode.UNSUPPORTED_CONTENT)
                elif child.name == "br":
                    pieces.append("\n")
                elif child.name == "img":
                    pieces.append(HtmlTextReader._alt(child))
                else:
                    collect(child)

        collect(element)
        content = "".join(pieces)
        # As in browsers, one newline directly after <pre> is not content.
        content = (
            content.removeprefix("\r\n")
            if content.startswith("\r\n")
            else content.removeprefix("\n")
        )
        return content if content.strip() else None

    @staticmethod
    def _table_text(
        table: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        lines: list[str] = []
        for child in table.children:
            if (
                HtmlTextReader._is_tag(child)
                and child.name == "caption"
                and id(child) not in excluded
            ):
                if (
                    caption := HtmlTextReader._element_text_inline(
                        child, excluded, notes
                    )
                ) is not None:
                    lines.append(caption)
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table or not HtmlTextReader._visible_in(
                row, table, notes
            ):
                continue
            if id(row) in excluded:
                continue
            cells = [
                HtmlTextReader._element_text_inline(cell, excluded, notes) or ""
                for cell in row.children
                if HtmlTextReader._is_tag(cell)
                and cell.name in ("td", "th")
                and id(cell) not in excluded
                and HtmlTextReader._visible_in(cell, table, notes)
            ]
            if any(cell.strip() for cell in cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines) if lines else None

    @staticmethod
    def _element_text_inline(
        element: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        """Cell and caption text on one line, so each table row stays one line."""
        text = TextLines()
        HtmlTextReader._render(element, text, excluded, notes)
        content = text.result()
        if content is None:
            return None
        return " ".join(line.strip() for line in content.split("\n") if line.strip())

    @staticmethod
    def _visible_in(element: Any, table: Any, notes: set[FindingCode]) -> bool:
        node: Any = element
        while node is not None and node is not table:
            if node.has_attr("hidden"):
                notes.add(FindingCode.HIDDEN_SKIPPED)
                return False
            if node.name in HtmlTextReader._SKIPPED:
                return False
            node = node.parent
        return True

    @staticmethod
    def _resolve(document: Any, indexes: Sequence[int]) -> Any:
        node = document
        for index in indexes:
            children = [
                child for child in node.children if HtmlTextReader._is_tag(child)
            ]
            if index >= len(children):
                return None
            node = children[index]
        return node

    @staticmethod
    def _lineage(element: Any) -> list[Any]:
        """The element and its ancestor elements, excluding the document root."""
        chain: list[Any] = []
        node = element
        while node is not None and node.parent is not None:
            chain.append(node)
            node = node.parent
        return chain

    @staticmethod
    def _alt(element: Any) -> str:
        alt = element.get("alt")
        return alt if isinstance(alt, str) else ""

    @staticmethod
    def _is_tag(node: Any) -> bool:
        from bs4.element import Tag

        return isinstance(node, Tag)

    @staticmethod
    def _is_text(node: Any) -> bool:
        from bs4.element import (
            CData,
            Comment,
            Declaration,
            Doctype,
            NavigableString,
            ProcessingInstruction,
        )

        return isinstance(node, NavigableString) and not isinstance(
            node, (CData, Comment, Declaration, Doctype, ProcessingInstruction)
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
