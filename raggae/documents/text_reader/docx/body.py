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

    _W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    _MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
    _WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    _M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    _V = "{urn:schemas-microsoft-com:vml}"
    _R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

    _DRAWINGS = frozenset({f"{_W}drawing", f"{_W}pict"})
    # Containers whose children are ordinary content in the accepted view.
    _TRANSPARENT = frozenset(
        {
            f"{_W}hyperlink",
            f"{_W}smartTag",
            f"{_W}customXml",
            f"{_W}fldSimple",
            f"{_W}ins",
            f"{_W}moveTo",
            f"{_W}dir",
            f"{_W}bdo",
            f"{_W}sdtContent",
            f"{_W}sdt",
        }
    )
    _DELETED = frozenset({f"{_W}del", f"{_W}moveFrom"})
    _REVISIONS = _DELETED | {f"{_W}ins", f"{_W}moveTo"}
    _UNSUPPORTED = frozenset(
        {f"{_W}object", f"{_W}altChunk", f"{_M}oMath", f"{_M}oMathPara"}
    )
    _RUN_TEXT = {
        f"{_W}t": None,
        f"{_W}tab": "\t",
        f"{_W}ptab": "\t",
        f"{_W}br": "\n",
        f"{_W}cr": "\n",
        f"{_W}noBreakHyphen": "-",
    }
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
                DocxTextReader._resolve(body, location.child_indexes)
                if location.part == part
                else None
            )
            if element is None:
                findings.append(
                    DocxTextReader._finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's element location does not match this document.",
                        region,
                    )
                )
            elif reason := DocxTextReader._unreadable(element):
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
            if any(
                ancestor.tag in DocxTextReader._DELETED
                for ancestor in element.iterancestors()
            ):
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
        if tag == f"{DocxTextReader._W}p":
            return DocxTextReader._paragraph_text(element, excluded, notes)
        if tag == f"{DocxTextReader._W}tbl":
            return DocxTextReader._table_text(element, excluded, notes)
        if tag in DocxTextReader._DRAWINGS:
            return DocxTextReader._alt_text(element)
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
            if tag == f"{DocxTextReader._W}p":
                text = DocxTextReader._paragraph_text(child, excluded, notes)
                if text.strip():
                    texts.append(text)
            elif tag == f"{DocxTextReader._W}tbl":
                if (
                    text := DocxTextReader._table_text(child, excluded, notes)
                ) is not None:
                    texts.append(text)
            elif tag in (
                f"{DocxTextReader._W}sdt",
                f"{DocxTextReader._W}sdtContent",
                f"{DocxTextReader._W}customXml",
            ):
                texts.extend(DocxTextReader._body_texts(child, excluded, notes))
            elif tag in DocxTextReader._UNSUPPORTED:
                notes.add(FindingCode.UNSUPPORTED_CONTENT)
        return texts

    @staticmethod
    def _paragraph_text(
        paragraph: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str:
        pieces: list[str] = []

        def visit(node: Any) -> None:
            for child in node:
                tag = child.tag
                if id(child) in excluded or not isinstance(tag, str):
                    continue
                if tag in DocxTextReader._REVISIONS:
                    notes.add(FindingCode.REVISIONS_PRESENT)
                if tag in DocxTextReader._DELETED:
                    continue
                if tag == f"{DocxTextReader._W}r":
                    visit_run(child)
                elif tag in DocxTextReader._TRANSPARENT:
                    visit(child)
                elif tag == f"{DocxTextReader._MC}AlternateContent":
                    choice = child.find(f"{DocxTextReader._MC}Choice")
                    if choice is not None:
                        visit(choice)
                elif tag in DocxTextReader._UNSUPPORTED:
                    notes.add(FindingCode.UNSUPPORTED_CONTENT)

        def visit_run(run: Any) -> None:
            for child in run:
                tag = child.tag
                if id(child) in excluded or not isinstance(tag, str):
                    continue
                if tag == f"{DocxTextReader._W}t":
                    pieces.append(child.text or "")
                elif tag in DocxTextReader._RUN_TEXT:
                    pieces.append(DocxTextReader._RUN_TEXT[tag] or "")
                elif tag in DocxTextReader._DRAWINGS:
                    if DocxTextReader._is_text_box(child):
                        notes.add(FindingCode.TEXT_BOXES_UNREAD)
                    elif alt := DocxTextReader._alt_text(child):
                        pieces.append(alt)
                elif tag == f"{DocxTextReader._MC}AlternateContent":
                    choice = child.find(f"{DocxTextReader._MC}Choice")
                    if choice is not None:
                        visit_run(choice)
                elif tag in DocxTextReader._UNSUPPORTED:
                    notes.add(FindingCode.UNSUPPORTED_CONTENT)

        visit(paragraph)
        return "".join(pieces)

    @staticmethod
    def _table_text(
        table: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        lines: list[str] = []
        for row in DocxTextReader._children(table, f"{DocxTextReader._W}tr"):
            properties = row.find(f"{DocxTextReader._W}trPr")
            if (
                properties is not None
                and properties.find(f"{DocxTextReader._W}del") is not None
            ):
                notes.add(FindingCode.REVISIONS_PRESENT)
                continue
            cells = [
                DocxTextReader._cell_text(cell, excluded, notes)
                for cell in DocxTextReader._children(row, f"{DocxTextReader._W}tc")
            ]
            if any(cell.strip() for cell in cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines) if lines else None

    @staticmethod
    def _cell_text(cell: Any, excluded: frozenset[int], notes: set[FindingCode]) -> str:
        """One line per cell, so each table row stays on one line."""
        parts: list[str] = []
        for child in DocxTextReader._children(
            cell, f"{DocxTextReader._W}p", f"{DocxTextReader._W}tbl"
        ):
            text = (
                DocxTextReader._paragraph_text(child, excluded, notes)
                if child.tag == f"{DocxTextReader._W}p"
                else DocxTextReader._table_text(child, excluded, notes)
            )
            if text is not None and text.strip():
                parts.append(" ".join(text.split()))
        return " ".join(parts)

    @staticmethod
    def _children(node: Any, *tags: str) -> list[Any]:
        """Direct children with these tags, looking through content-control wrappers."""
        found: list[Any] = []
        for child in node:
            if child.tag in tags:
                found.append(child)
            elif child.tag in (
                f"{DocxTextReader._W}sdt",
                f"{DocxTextReader._W}sdtContent",
                f"{DocxTextReader._W}customXml",
            ):
                found.extend(DocxTextReader._children(child, *tags))
        return found

    @staticmethod
    def _alt_text(drawing: Any) -> str | None:
        if DocxTextReader._is_text_box(drawing):
            return None
        for properties in drawing.iter(f"{DocxTextReader._WP}docPr"):
            if description := properties.get("descr"):
                return " ".join(description.split()) or None
        for shape in drawing.iter(
            f"{DocxTextReader._V}shape", f"{DocxTextReader._V}rect"
        ):
            if description := shape.get("alt"):
                return " ".join(description.split()) or None
        return None

    @staticmethod
    def _is_text_box(drawing: Any) -> bool:
        return next(drawing.iter(f"{DocxTextReader._W}txbxContent"), None) is not None

    @staticmethod
    def _unreadable(element: Any) -> str | None:
        tag = element.tag
        if any(
            ancestor.tag == f"{DocxTextReader._MC}Fallback"
            for ancestor in element.iterancestors()
        ):
            return (
                "This element is an alternate representation of content read elsewhere."
            )
        if tag in DocxTextReader._DRAWINGS and DocxTextReader._is_text_box(element):
            return "Text boxes are not read."
        if (
            tag
            in (
                f"{DocxTextReader._W}p",
                f"{DocxTextReader._W}tbl",
                f"{DocxTextReader._W}sdt",
                f"{DocxTextReader._W}customXml",
            )
            or tag in DocxTextReader._DRAWINGS
        ):
            return None
        return "This element's content is outside what DocxTextReader reads."

    @staticmethod
    def _resolve(body: Any, indexes: Sequence[int]) -> Any:
        node = body
        for index in indexes:
            children = list(node)
            if index >= len(children):
                return None
            node = children[index]
        return node

    @staticmethod
    def _other_parts(document: Any, body: Any) -> set[FindingCode]:
        """Report other stories only when they hold text a caller might expect."""
        for tag in ("footnoteReference", "endnoteReference", "commentReference"):
            if next(body.iter(f"{DocxTextReader._W}{tag}"), None) is not None:
                return {FindingCode.OTHER_PARTS_UNREAD}
        related = document.part.related_parts
        for reference in body.iter(
            f"{DocxTextReader._W}headerReference", f"{DocxTextReader._W}footerReference"
        ):
            part = related.get(reference.get(f"{DocxTextReader._R}id"))
            element = getattr(part, "element", None)
            if element is not None and any(
                (text.text or "").strip()
                for text in element.iter(f"{DocxTextReader._W}t")
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
