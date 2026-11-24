"""Read slide shape text by slide, keeping each bullet with its shape."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any
from zipfile import BadZipFile, ZipFile
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from ..utils.sources import as_artifact, failure, nothing_selected, select_regions
from ...layout_analysis.models import (
    LayoutAnalysisResult,
    LayoutRegion,
    PptxShapeLocation,
)
from ...normalization.artifacts import Artifact
from ...shared.pptx_text import (
    A,
    P,
    body_text,
    hidden,
    resolve,
)
from ...shared.pptx_text import kind as shape_kind
from ...shared.pptx_text import slides as slide_list


class PptxTextReader:
    """Return plain-text blocks from slide shapes, with ``page_index`` = slide position.

    Without a layout, each text shape, table, or picture with alt text becomes
    one block, in the order shapes are stored on each slide (not visual reading
    order); group members are read individually. Each paragraph is one line;
    bullet symbols and numbering are not added. Tables put one row per line
    with cells joined by `` | ``. With a layout from ``PptxLayoutAnalyzer``, each
    selected shape region becomes one block. Hidden slides are read and
    reported. Charts, SmartArt, embedded objects, and layout/master text are
    not read. Speaker notes are read only with ``notes=True``, as one block
    after each slide's shape blocks.
    """

    _INFORMATIONAL = frozenset(
        {
            FindingCode.EMPTY,
            FindingCode.HIDDEN_SLIDE,
            FindingCode.NOTES_UNREAD,
            FindingCode.INHERITED_TEXT_UNREAD,
            FindingCode.UNSUPPORTED_CONTENT,
        }
    )
    _NOTES = {
        FindingCode.HIDDEN_SLIDE: "This slide is hidden in slide shows; its text was read.",
        FindingCode.NOTES_UNREAD: "This slide has speaker notes; they were not read (notes=False).",
        FindingCode.INHERITED_TEXT_UNREAD: (
            "Slide layouts or masters hold text, such as footers, that was not read."
        ),
        FindingCode.UNSUPPORTED_CONTENT: "Charts, SmartArt, or embedded objects were not read.",
    }

    def __init__(self, *, notes: bool = False) -> None:
        if not isinstance(notes, bool):
            raise TypeError("notes must be a bool")
        self._notes = notes

    @property
    def name(self) -> str:
        return "text_reader_pptx"

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
                FindingCode.DECODE_FAILED,
                "Source is not a readable presentation package.",
            )
        if len(set(names)) != len(names) or "[Content_Types].xml" not in names:
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source is not a readable Office document package.",
            )
        try:
            from lxml.etree import LxmlError
            from pptx import Presentation
            from pptx.exc import PackageNotFoundError
        except ImportError as error:
            raise ImportError(
                "PowerPoint text reading requires: pip install 'raggae[text-office]'"
            ) from error
        try:
            presentation = Presentation(BytesIO(data))
            slides = slide_list(presentation)
        except (
            BadZipFile,
            PackageNotFoundError,
            LxmlError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            return self._failure(
                FindingCode.DECODE_FAILED,
                "Source could not be opened as a presentation.",
            )
        try:
            if selected is None:
                return self._read_slides(slides)
            return self._read_regions(slides, selected)
        except RecursionError:
            return self._failure(
                FindingCode.STRUCTURE_UNSUPPORTED,
                "Shape groups are nested too deeply to read within Python's recursion limit.",
            )

    def _read_slides(self, slides: list[Any]) -> TextReadResult:
        blocks: list[TextBlock] = []
        findings: list[TextReadFinding] = []
        inherited = False
        for index, slide in enumerate(slides):
            notes: set[FindingCode] = set()
            if hidden(slide):
                notes.add(FindingCode.HIDDEN_SLIDE)
            for text in PptxTextReader._shapes_texts(slide.shapes, frozenset(), notes):
                blocks.append(TextBlock(text=text, page_index=index))
            speaker = PptxTextReader._notes_text(slide)
            if speaker is not None:
                if self._notes:
                    blocks.append(TextBlock(text=speaker, page_index=index))
                else:
                    notes.add(FindingCode.NOTES_UNREAD)
            findings.extend(
                PptxTextReader._note(code, page_index=index) for code in sorted(notes)
            )
            inherited = inherited or PptxTextReader._inherited_text(slide)
        if inherited:
            findings.append(PptxTextReader._note(FindingCode.INHERITED_TEXT_UNREAD))
        if not blocks:
            findings.append(
                PptxTextReader._finding(
                    FindingCode.EMPTY, "Presentation contains no readable slide text."
                )
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _read_regions(
        self, slides: list[Any], selected: tuple[LayoutRegion, ...]
    ) -> TextReadResult:
        by_part = {
            str(slide.part.partname).lstrip("/"): (index, slide)
            for index, slide in enumerate(slides)
        }
        findings: list[TextReadFinding] = []
        resolved: list[tuple[LayoutRegion, int, Any]] = []
        for region in selected:
            location = region.location
            if not isinstance(location, PptxShapeLocation):
                findings.append(
                    PptxTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "This region has no slide shape location.",
                        region=region,
                    )
                )
                continue
            found = by_part.get(location.part)
            shape = None if found is None else resolve(found[1], location.shape_path)
            if found is None or shape is None or shape.shape_id != location.shape_id:
                findings.append(
                    PptxTextReader._finding(
                        FindingCode.REGION_UNRESOLVED,
                        "The region's shape location does not match this presentation.",
                        region=region,
                        page_index=None if found is None else found[0],
                    )
                )
                continue
            if shape_kind(shape) == "unsupported":
                findings.append(
                    PptxTextReader._finding(
                        FindingCode.SELECTION_UNSUPPORTED,
                        "Charts, SmartArt, and embedded objects are not read.",
                        region=region,
                        page_index=found[0],
                    )
                )
                continue
            resolved.append((region, found[0], shape))

        counts: dict[int, int] = {}
        for _, _, shape in resolved:
            counts[id(shape.element)] = counts.get(id(shape.element), 0) + 1
        owners = frozenset(id(shape.element) for _, _, shape in resolved)
        hidden_reported: set[int] = set()
        blocks: list[TextBlock] = []
        completed = 0
        for region, index, shape in resolved:
            if counts[id(shape.element)] > 1:
                findings.append(
                    PptxTextReader._finding(
                        FindingCode.SELECTION_OVERLAP,
                        "Another selected region locates the same shape; neither was read.",
                        region=region,
                        page_index=index,
                    )
                )
                continue
            completed += 1
            if index not in hidden_reported and hidden(slides[index]):
                hidden_reported.add(index)
                findings.append(
                    PptxTextReader._note(FindingCode.HIDDEN_SLIDE, page_index=index)
                )
            notes: set[FindingCode] = set()
            text = PptxTextReader._shape_text(
                shape, owners - {id(shape.element)}, notes
            )
            findings.extend(
                PptxTextReader._note(code, region=region, page_index=index)
                for code in sorted(notes)
            )
            if text is None:
                findings.append(
                    PptxTextReader._finding(
                        FindingCode.EMPTY,
                        "Region contains no readable text.",
                        region=region,
                        page_index=index,
                    )
                )
            else:
                blocks.append(
                    TextBlock(text=text, page_index=index, region_id=region.id)
                )
        incomplete = any(f.code not in PptxTextReader._INFORMATIONAL for f in findings)
        return TextReadResult(
            reader=self.name,
            outcome=("partial" if incomplete else "success") if completed else "failed",
            blocks=tuple(blocks),
            findings=tuple(findings),
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _shapes_texts(
        shapes: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> list[str]:
        """Block texts for shapes in storage order, reading group members individually."""
        texts: list[str] = []
        for shape in shapes:
            if id(shape.element) in excluded:
                continue
            kind = shape_kind(shape)
            if kind == "group":
                texts.extend(
                    PptxTextReader._shapes_texts(shape.shapes, excluded, notes)
                )
            elif kind == "unsupported":
                notes.add(FindingCode.UNSUPPORTED_CONTENT)
            elif (
                text := PptxTextReader._shape_text(shape, excluded, notes)
            ) is not None:
                texts.append(text)
        return texts

    @staticmethod
    def _shape_text(
        shape: Any, excluded: frozenset[int], notes: set[FindingCode]
    ) -> str | None:
        kind = shape_kind(shape)
        if kind == "group":
            texts = PptxTextReader._shapes_texts(shape.shapes, excluded, notes)
            return "\n".join(texts) if texts else None
        if kind == "picture":
            names = shape.element.xpath("./p:nvPicPr/p:cNvPr")
            alt = " ".join((names[0].get("descr") or "").split()) if names else ""
            return alt or None
        if kind == "table":
            return PptxTextReader._table_text(shape.table._tbl)
        if kind == "text":
            body = shape.element.find(f"{P}txBody")
            return None if body is None else body_text(body)
        if kind == "unsupported":
            notes.add(FindingCode.UNSUPPORTED_CONTENT)
        return None

    @staticmethod
    def _table_text(table: Any) -> str | None:
        lines: list[str] = []
        for row in table.findall(f"{A}tr"):
            cells: list[str] = []
            for cell in row.findall(f"{A}tc"):
                if cell.get("hMerge") in ("1", "true"):
                    continue  # Covered by the merged cell to its left.
                if cell.get("vMerge") in ("1", "true"):
                    cells.append("")  # Covered by the merged cell above.
                    continue
                body = cell.find(f"{A}txBody")
                text = None if body is None else body_text(body)
                cells.append("" if text is None else " ".join(text.split()))
            if any(cell.strip() for cell in cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines) if lines else None

    @staticmethod
    def _notes_text(slide: Any) -> str | None:
        if not slide.has_notes_slide:
            return None
        frame = slide.notes_slide.notes_text_frame
        return None if frame is None else body_text(frame._txBody)

    @staticmethod
    def _inherited_text(slide: Any) -> bool:
        layout = slide.slide_layout
        return any(
            not shape.is_placeholder
            and shape.has_text_frame
            and shape.text_frame.text.strip()
            for shape in list(layout.shapes) + list(layout.slide_master.shapes)
        )

    @staticmethod
    def _finding(
        code: FindingCode,
        message: str,
        *,
        region: LayoutRegion | None = None,
        page_index: int | None = None,
    ) -> TextReadFinding:
        return TextReadFinding(
            code=code,
            message=message,
            region_id=None if region is None else region.id,
            page_index=page_index,
        )

    @staticmethod
    def _note(
        code: FindingCode,
        *,
        region: LayoutRegion | None = None,
        page_index: int | None = None,
    ) -> TextReadFinding:
        return PptxTextReader._finding(
            code, PptxTextReader._NOTES[code], region=region, page_index=page_index
        )
