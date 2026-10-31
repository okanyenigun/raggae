"""Locate HTML elements using a fixed, non-executing parser."""

import warnings
from pathlib import Path
from stat import S_ISREG
from typing import Any
from .policy import HtmlLayoutPolicy
from ...models import (
    HtmlDocumentLocation,
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    RegionKind,
)
from ...utils import resolve_source
from ....normalization.artifacts import Artifact

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_TEXT_BLOCKS = frozenset({"p", "pre", "li", "dt", "dd", "blockquote", "figcaption"})
_CONTAINERS = frozenset(
    {
        "div",
        "section",
        "article",
        "main",
        "nav",
        "aside",
        "header",
        "footer",
        "ul",
        "ol",
        "dl",
    }
)
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


class HtmlLayoutAnalyzer:
    """Map native UTF-8 HTML structure, never CSS/browser layout or final text."""

    def __init__(self, policy: HtmlLayoutPolicy | None = None) -> None:
        self._policy = policy if policy is not None else HtmlLayoutPolicy()

    @property
    def policy(self) -> HtmlLayoutPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_html"

    def analyze(self, source: Path | str | bytes | Artifact) -> LayoutAnalysisResult:
        source = resolve_source(source)
        try:
            if source.path is not None:
                path = source.path
                info = path.stat()
                if not S_ISREG(info.st_mode):
                    return self._failure(
                        "source_unreadable", "Source must be a regular file."
                    )
                if info.st_size > self._policy.max_source_bytes:
                    return self._failure(
                        "source_too_large", "Source exceeds the byte limit."
                    )
                with path.open("rb") as stream:
                    data = stream.read(self._policy.max_source_bytes + 1)
            else:
                assert source.data is not None
                data = source.data
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure("source_too_large", "Source exceeds the byte limit.")
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return self._failure(
                "encoding_unsupported",
                "Source must be UTF-8; convert other encodings explicitly before analysis.",
            )
        if "\x00" in text:
            return self._failure(
                "decode_failed",
                "Source contains null characters and cannot be analyzed as UTF-8 HTML.",
            )
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise ImportError(
                "HTML layout requires: pip install 'raggae[layout-html]'"
            ) from error
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", UserWarning)
                document = BeautifulSoup(text, "html.parser")
        except (ValueError, AssertionError, RecursionError):
            return self._failure("decode_failed", "Source could not be parsed as HTML.")
        return self._locate(document, parser_warning=bool(captured))

    def _locate(self, document: Any, *, parser_warning: bool) -> LayoutAnalysisResult:
        from bs4.element import (
            Comment,
            Declaration,
            Doctype,
            NavigableString,
            ProcessingInstruction,
            Tag,
        )

        surface = LayoutSurface(
            id_="html", kind="flow", location=HtmlDocumentLocation(element="document")
        )
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []
        reported: set[str] = set()

        def issue(code: str, message: str) -> None:
            if code not in reported:
                findings.append(
                    LayoutFinding(
                        code=f"layout.html.{code}",
                        surface_id=surface.id_,
                        message=message,
                    )
                )
                reported.add(code)

        def direct_text(node: Any) -> bool:
            return any(
                isinstance(child, NavigableString)
                and not isinstance(
                    child, (Comment, Declaration, Doctype, ProcessingInstruction)
                )
                and str(child).strip()
                for child in node.children
            )

        if parser_warning:
            issue(
                "parser_coverage",
                "The parser reported a source interpretation warning; browser interpretation may differ.",
            )
        if direct_text(document):
            issue(
                "unlocated_text",
                "Text outside an element has no element locator and was not mapped.",
            )
        # Preserve element-only paths, including skipped head/script elements.
        roots = [child for child in document.children if isinstance(child, Tag)]
        pending: list[tuple[Tag, tuple[int, ...], bool, bool, bool]] = [
            (node, (index,), False, False, False)
            for index, node in reversed(list(enumerate(roots)))
        ]
        visited = 0
        while pending:
            if visited >= self._policy.max_elements:
                issue(
                    "element_limit",
                    "Remaining elements were not analyzed: element limit reached.",
                )
                break
            if len(regions) >= self._policy.max_regions:
                issue(
                    "region_limit",
                    "Remaining elements were not analyzed: region limit reached.",
                )
                break
            node, path, text_owned, in_table, in_head = pending.pop()
            visited += 1
            if len(path) > self._policy.max_depth:
                issue(
                    "depth_limit",
                    "A deeply nested subtree was not analyzed: depth limit reached.",
                )
                continue
            tag = node.name
            if (
                tag == "style"
                or node.has_attr("style")
                or (tag == "link" and "stylesheet" in (node.get("rel") or ()))
            ):
                issue(
                    "styling_uninterpreted",
                    "CSS was not applied; visibility and rendered order may differ from native structure.",
                )
            if node.has_attr("hidden"):
                issue(
                    "hidden_content",
                    "Stored hidden content is mapped without simulating browser visibility.",
                )
            if tag == "script":
                issue(
                    "dynamic_content",
                    "Scripts were not executed; dynamically generated content was not mapped.",
                )
                continue
            if tag in ("template", "noscript"):
                issue(
                    "conditional_content",
                    "Conditional or inert template content was not mapped.",
                )
                continue
            if tag == "style":
                continue
            in_head = in_head or tag == "head"
            kind: RegionKind | None = None
            owns_children = text_owned
            if not in_head:
                if tag in _UNSUPPORTED:
                    kind = "unknown"
                    issue(
                        "unsupported_content",
                        "An embedded, interactive, or vector element was located without interpreting its contents.",
                    )
                elif tag == "img":
                    kind = "image"
                elif tag == "table" and not in_table:
                    kind = "table"
                elif not in_table and tag in _HEADINGS | _TEXT_BLOCKS:
                    if text_owned:
                        issue(
                            "nested_text_blocks",
                            "Nested text blocks are covered by an ancestor region; browser parsing or block boundaries may differ.",
                        )
                    elif next(node.stripped_strings, None) is not None:
                        kind = (
                            "heading"
                            if tag in _HEADINGS
                            else "caption"
                            if tag == "figcaption"
                            else "text"
                        )
                        owns_children = True
                elif not in_table and not text_owned and direct_text(node):
                    if (
                        node.find(
                            list(_HEADINGS | _TEXT_BLOCKS | _CONTAINERS | {"table"})
                        )
                        is not None
                    ):
                        kind = "unknown"
                        issue(
                            "mixed_container_text",
                            "A container mixes direct text with child blocks; its enclosing region can overlap child regions.",
                        )
                    else:
                        kind, owns_children = "text", True
            if kind is not None:
                regions.append(
                    LayoutRegion(
                        id=f"html-r{len(regions) + 1}",
                        surface_id=surface.id_,
                        kind=kind,
                        location=HtmlElementLocation(child_indexes=path, tag=tag),
                        native_order=len(regions),
                    )
                )
            if tag in _UNSUPPORTED:
                continue
            children = [child for child in node.children if isinstance(child, Tag)]
            pending.extend(
                (
                    child,
                    path + (index,),
                    owns_children,
                    in_table or tag == "table",
                    in_head,
                )
                for index, child in reversed(list(enumerate(children)))
            )
        return LayoutAnalysisResult(
            outcome="partial" if findings else "success",
            surfaces=(surface,),
            regions=tuple(regions),
            findings=tuple(findings),
        )

    @staticmethod
    def _failure(code: str, message: str) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            outcome="failed",
            findings=(LayoutFinding(code=f"layout.html.{code}", message=message),),
        )
