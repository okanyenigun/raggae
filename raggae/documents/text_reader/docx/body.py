"""Read Word main-body text as if tracked changes were accepted."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    DocxElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
)
from ...shared.docx_text import (
    DELETED,
    DRAWINGS,
    R,
    UNSUPPORTED,
    W,
    WRAPPERS,
    alt_text,
    paragraph_text,
    resolve,
    table_text,
    unreadable,
)
from ...normalization.artifacts import Artifact


class DocxTextReader:
    """Return plain-text blocks from the main body of a Word document.

    Without a layout, each nonempty body paragraph and each table becomes one
    block; content controls are read as ordinary body content. With a layout
    from ``DocxLayoutAnalyzer``, each selected region becomes one block, and an
    image inside a selected paragraph or table belongs only to its own region.
    Tracked changes are read as if accepted. Tables put one row per line with
    cells joined by `` | ``; a merged cell is read once. Image alt text is read.
    Text boxes, headers, footers, footnotes, endnotes, and comments are not
    read; findings say when they hold text. List numbering is not added.
    """

    _INFORMATIONAL = frozenset(
        {
            FindingCode.EMPTY,
            FindingCode.REVISIONS_PRESENT,
            FindingCode.OTHER_PARTS_UNREAD,
            FindingCode.TEXT_BOXES_UNREAD,
            FindingCode.UNSUPPORTED_CONTENT,
        }
    )
    _NOTES = {
        FindingCode.REVISIONS_PRESENT: (
            "Tracked changes were read as if accepted: insertions kept, deletions omitted."
        ),
        FindingCode.OTHER_PARTS_UNREAD: (
            "Headers, footers, footnotes, endnotes, or comments contain text that "
            "was not read; only the main body is read."
        ),
        FindingCode.TEXT_BOXES_UNREAD: "Text boxes were not read.",
        FindingCode.UNSUPPORTED_CONTENT: (
            "Embedded objects, equations, or imported content were not read."
        ),
    }

    @property
    def name(self) -> str:
        return "text_reader_docx"

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
        try:
            if artifact.path is not None and not S_ISREG(artifact.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = artifact.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        try:
            with ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
        except BadZipFile:
            return self._failure(
                FindingCode.DECODE_FAILED, "Source is not a readable DOCX package."
            )
        if len(set(names)) != len(names) or "[Content_Types].xml" not in names:
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source is not a readable Office document package.",
            )
        try:
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
            from lxml.etree import LxmlError
        except ImportError as error:
            raise ImportError(
                "Word text reading requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            document = Document(BytesIO(data))
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            # python-docx raises these on some malformed package parts.
            AttributeError,
            TypeError,
        ):
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source could not be opened as a Word document.",
            )
        try:
            if selected is None:
                return self._read_body(document)
            return self._read_regions(document, selected)
        except RecursionError:
            return self._failure(
                FindingCode.STRUCTURE_UNSUPPORTED,
                "Word structure is too deeply nested to read within Python's recursion limit.",
            )

    def _read_body(self, document: Any) -> TextReadResult:
        notes: set[FindingCode] = set()
        body = document.element.body
        blocks = [
            TextBlock(text=text, page_index=0)
            for text in DocxTextReader._body_texts(body, frozenset(), notes)
        ]
        notes |= DocxTextReader._other_parts(document, body)
        findings = [DocxTextReader._note(code) for code in sorted(notes)]
        if not blocks:
            findings.append(
                DocxTextReader._finding(
                    FindingCode.EMPTY, "Document body contains no readable text."
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
        part = str(document.part.partname).lstrip("/")
        body = document.element.body
        findings: list[TextReadFinding] = []
        resolved: list[tuple[LayoutRegion, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, DocxElementLocation):
                findings.append(
                    DocxTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This region has no Word element location.",
                        region,
                    )
                )
                continue
            element = (
                resolve(body, location.child_indexes) if location.part == part else None
            )
            if element is None:
                findings.append(
                    DocxTextReader._finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's element location does not match this document.",
                        region,
                    )
                )
            elif reason := unreadable(element):
                findings.append(
                    DocxTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED, reason, region
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
                    DocxTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Another selected region locates the same element; neither was read.",
                        region,
                    )
                )
                continue
            completed += 1
            notes: set[FindingCode] = set()
            if any(ancestor.tag in DELETED for ancestor in element.iterancestors()):
                notes.add(FindingCode.REVISIONS_PRESENT)
                text = None
            else:
                text = DocxTextReader._element_text(
                    element, owners - {id(element)}, notes
                )
            findings.extend(
                DocxTextReader._note(code, region) for code in sorted(notes)
            )
            if text is None or not text.strip():
                findings.append(
                    DocxTextReader._finding(
                        FindingCode.EMPTY, "Region contains no readable text.", region
                    )
                )
            else:
                blocks.append(TextBlock(text=text, page_index=0, region_id=region.id))
        incomplete = any(f.code not in DocxTextReader._INFORMATIONAL for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _element_text(
        element: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        tag = element.tag
        if tag == f"{W}p":
            return paragraph_text(element, excluded, notes)
        if tag == f"{W}tbl":
            return table_text(element, excluded, notes)
        if tag in DRAWINGS:
            return alt_text(element)
        # A body-level content control or custom-XML wrapper: its blocks, one per line.
        texts = DocxTextReader._body_texts(element, excluded, notes)
        return "\n".join(texts) if texts else None

    @staticmethod
    def _body_texts(
        container: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> list[str]:
        """Paragraph and table texts at body level, looking through wrappers."""
        texts: list[str] = []
        for child in container:
            tag = child.tag
            if id(child) in excluded:
                continue
            if tag == f"{W}p":
                text = paragraph_text(child, excluded, notes)
                if text.strip():
                    texts.append(text)
            elif tag == f"{W}tbl":
                if (text := table_text(child, excluded, notes)) is not None:
                    texts.append(text)
            elif tag in WRAPPERS:
                texts.extend(DocxTextReader._body_texts(child, excluded, notes))
            elif tag in UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)
        return texts

    @staticmethod
    def _other_parts(document: Any, body: Any) -> set[FindingCode]:
        """Report other stories only when they hold text a caller might expect."""
        for tag in ("footnoteReference", "endnoteReference", "commentReference"):
            if next(body.iter(f"{W}{tag}"), None) is not None:
                return {FindingCode.OTHER_PARTS_UNREAD}
        related = document.part.related_parts
        for reference in body.iter(f"{W}headerReference", f"{W}footerReference"):
            part = related.get(reference.get(f"{R}id"))
            element = getattr(part, "element", None)
            if element is not None and any(
                (text.text or "").strip() for text in element.iter(f"{W}t")
            ):
                return {FindingCode.OTHER_PARTS_UNREAD}
        return set()

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
        return DocxTextReader._finding(code, DocxTextReader._NOTES[code], region)
