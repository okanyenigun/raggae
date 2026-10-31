"""Locate native Word body structure without rendering pages or extracting text."""

from pathlib import Path
from io import BytesIO
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from .policy import DocxLayoutPolicy
from ...models import (
    DocxBodyLocation,
    DocxElementLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    RegionKind,
)
from ...utils import resolve_source
from ....normalization.artifacts import Artifact


class DocxLayoutAnalyzer:
    """Map main-body paragraphs, styled headings, tables, and embedded images.

    Tables are atomic blocks. Image locators can point inside a table or paragraph;
    native_order is traversal sequence, not rendered or semantic reading order.
    """

    def __init__(self, policy: DocxLayoutPolicy | None = None) -> None:
        self._policy = policy if policy is not None else DocxLayoutPolicy()

    @property
    def policy(self) -> DocxLayoutPolicy:
        return self._policy

    @property
    def name(self) -> str:
        return "layout_analysis_docx"

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
                        "source_too_large", "Source exceeds the configured byte limit."
                    )
                with path.open("rb") as stream:
                    data = stream.read(self._policy.max_source_bytes + 1)
            else:
                assert source.data is not None
                data = source.data
        except OSError:
            return self._failure("source_unreadable", "Source could not be read.")
        if len(data) > self._policy.max_source_bytes:
            return self._failure(
                "source_too_large", "Source exceeds the configured byte limit."
            )
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > self._policy.max_archive_entries
                    or sum(entry.file_size for entry in entries)
                    > self._policy.max_expanded_bytes
                ):
                    return self._failure(
                        "archive_limit",
                        "ZIP entry count or declared expanded size exceeds the limit.",
                    )
                if len({entry.filename for entry in entries}) != len(entries):
                    return self._failure(
                        "decode_failed", "ZIP package contains duplicate part names."
                    )
                if "[Content_Types].xml" not in archive.namelist():
                    return self._failure(
                        "decode_failed", "Source is not an Office document package."
                    )
        except BadZipFile:
            return self._failure(
                "decode_failed", "Source is not a readable DOCX package."
            )
        try:
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
            from lxml.etree import LxmlError
        except ImportError as error:
            raise ImportError(
                "Word layout requires: pip install 'raggae[layout-docx]'"
            ) from error
        try:
            document = Document(BytesIO(data))
        except (BadZipFile, PackageNotFoundError, LxmlError, ValueError, KeyError):
            return self._failure(
                "decode_failed", "Source could not be opened as a Word document."
            )
        return self._locate(document)

    def _locate(self, document: Any) -> LayoutAnalysisResult:
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph

        part = str(document.part.partname).lstrip("/")
        surface = LayoutSurface(
            id_="body", kind="flow", location=DocxBodyLocation(part=part)
        )
        regions: list[LayoutRegion] = []
        findings: list[LayoutFinding] = []

        def add(kind: RegionKind, indexes: tuple[int, ...]) -> None:
            regions.append(
                LayoutRegion(
                    id=f"body-r{len(regions) + 1}",
                    surface_id=surface.id_,
                    kind=kind,
                    location=DocxElementLocation(part=part, child_indexes=indexes),
                    native_order=len(regions),
                )
            )

        for index, block in enumerate(document.element.body):
            if block.tag == qn("w:sectPr"):
                continue
            if len(regions) >= self._policy.max_regions:
                findings.append(
                    LayoutFinding(
                        code="layout.docx.region_limit",
                        surface_id="body",
                        message="Remaining body blocks were not analyzed: region limit reached.",
                    )
                )
                break
            if block.tag == qn("w:tbl"):
                add("table", (index,))
            elif block.tag == qn("w:p"):
                paragraph = Paragraph(block, document)
                if paragraph.text.strip():
                    add("heading" if self._is_heading(paragraph) else "text", (index,))
            else:
                add("unknown", (index,))
                findings.append(
                    LayoutFinding(
                        code="layout.docx.unsupported_block",
                        surface_id="body",
                        message="A body container was located but its contents were not interpreted.",
                    )
                )
                continue
            # Iterative element traversal preserves exact child-index locators,
            # including pictures inside tables, without duplicating cell text.
            pending: list[tuple[Any, tuple[int, ...]]] = [(block, (index,))]
            while pending:
                node, indexes = pending.pop()
                if node.tag in (qn("w:drawing"), qn("w:pict")):
                    if len(regions) >= self._policy.max_regions:
                        findings.append(
                            LayoutFinding(
                                code="layout.docx.region_limit",
                                surface_id="body",
                                message="Additional drawings were not analyzed: region limit reached.",
                            )
                        )
                        break
                    blips = list(node.iter(qn("a:blip")))
                    embedded = blips[0].get(qn("r:embed")) if len(blips) == 1 else None
                    relation = document.part.rels.get(embedded) if embedded else None
                    image = (
                        relation is not None
                        and not relation.is_external
                        and relation.target_part.content_type.startswith("image/")
                    )
                    add("image" if image else "unknown", indexes)
                    if not image or any(True for _ in node.iter(qn("wp:anchor"))):
                        findings.append(
                            LayoutFinding(
                                code="layout.docx.drawing_partial",
                                surface_id="body",
                                region_id=regions[-1].id,
                                message="Drawing placement or contents require rendering or additional interpretation.",
                            )
                        )
                    continue
                pending.extend(
                    (child, indexes + (child_index,))
                    for child_index, child in reversed(list(enumerate(node)))
                )
        unsupported = {
            qn("w:ins"),
            qn("w:del"),
            qn("w:sdt"),
            qn("w:altChunk"),
            qn("w:footnoteReference"),
            qn("w:endnoteReference"),
            qn("w:object"),
            qn("w:fldChar"),
        }
        if any(node.tag in unsupported for node in document.element.body.iter()):
            findings.append(
                LayoutFinding(
                    code="layout.docx.additional_content",
                    surface_id="body",
                    message="Revisions, fields, embedded objects, or other unsupported structures may need additional readers.",
                )
            )
        if any(
            str(relation.reltype).rsplit("/", 1)[-1]
            in ("header", "footer", "comments", "footnotes", "endnotes")
            for relation in document.part.rels.values()
        ):
            findings.append(
                LayoutFinding(
                    code="layout.docx.other_stories",
                    surface_id="body",
                    message="Only the main body was analyzed; auxiliary stories were not mapped.",
                )
            )
        return LayoutAnalysisResult(
            outcome="partial" if findings else "success",
            surfaces=(surface,),
            regions=tuple(regions),
            findings=tuple(findings),
        )

    @staticmethod
    def _is_heading(paragraph: Any) -> bool:
        from docx.oxml.ns import qn

        direct = paragraph._p.find("./" + qn("w:pPr") + "/" + qn("w:outlineLvl"))
        if direct is not None and direct.get(qn("w:val")) in tuple(
            str(level) for level in range(9)
        ):
            return True
        style, visited = paragraph.style, set()
        while style is not None and style.style_id not in visited:
            visited.add(style.style_id)
            if style.style_id == "Title" or style.style_id in {
                f"Heading{level}" for level in range(1, 10)
            }:
                return True
            style = style.base_style
        return False

    @staticmethod
    def _failure(code: str, message: str) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            outcome="failed",
            findings=(LayoutFinding(code=f"layout.docx.{code}", message=message),),
        )
