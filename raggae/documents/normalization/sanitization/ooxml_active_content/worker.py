"""Remove known VBA parts and external relationships from OOXML ZIP packages."""

import copy
import posixpath
import zipfile
from collections import Counter
from typing import BinaryIO, Literal, Unpack, overload
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from .models import (
    OoxmlActiveContentCategory,
    OoxmlActiveContentFinding,
    OoxmlActiveContentIssue,
    OoxmlActiveContentOutcome,
    OoxmlActiveContentReport,
)
from ...io import SharedInputOutputHandler
from ...utils import ooxml_relationship_part, resolve_ooxml_target
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_MACRO_NAMES = frozenset({"vbaproject.bin", "vbaprojectsignature.bin", "vbadata.xml"})
_MAIN_PARTS: dict[str, Literal["docx", "xlsx", "pptx"]] = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}


class OoxmlActiveContentSanitizer:
    """Opt-in package removal, not spreadsheet formula or metadata sanitization."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_ooxml_active_content"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[OoxmlActiveContentReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[OoxmlActiveContentReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[OoxmlActiveContentReport]:
        request = resolve_request(request, kwargs)

        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(
        self, source: BinaryIO, output: BinaryIO
    ) -> OoxmlActiveContentReport:
        try:
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if len({info.filename for info in infos}) != len(infos):
                    raise ValueError("duplicate package parts")
                entries = [(info, archive.read(info)) for info in infos]
                comment = archive.comment
            payloads = {info.filename: data for info, data in entries}
            if "[Content_Types].xml" not in payloads:
                return self._unsupported()
            content_types = ET.fromstring(payloads["[Content_Types].xml"])
            if content_types.tag != f"{{{_CT}}}Types":
                raise ValueError("invalid content types root")
            families: list[Literal["docx", "xlsx", "pptx"]] = [
                family for main, family in _MAIN_PARTS.items() if main in payloads
            ]
            if len(families) != 1:
                return self._unsupported()
            document_format = families[0]
            updated, removals = self._remove(entries, content_types)
        except (
            zipfile.BadZipFile,
            ET.ParseError,
            ValueError,
            RuntimeError,
            NotImplementedError,
        ):
            return self._failure(
                OoxmlActiveContentFinding.DECODE_FAILED,
                "OOXML package could not be read strictly.",
            )
        if not removals:
            return OoxmlActiveContentReport(
                outcome=OoxmlActiveContentOutcome.NO_CHANGE,
                document_format=document_format,
            )
        try:
            with zipfile.ZipFile(output, "w") as archive:
                archive.comment = comment
                for info, data in updated:
                    archive.writestr(copy.copy(info), data)
        except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError):
            return self._failure(
                OoxmlActiveContentFinding.ENCODE_FAILED,
                "Sanitized OOXML package could not be encoded.",
            )
        return OoxmlActiveContentReport(
            outcome=OoxmlActiveContentOutcome.CHANGED,
            document_format=document_format,
            removals=dict(removals),
        )

    @staticmethod
    def _remove(
        entries: list[tuple[zipfile.ZipInfo, bytes]],
        content_types: ET.Element,
    ) -> tuple[
        list[tuple[zipfile.ZipInfo, bytes]], Counter[OoxmlActiveContentCategory]
    ]:
        removed: Counter[OoxmlActiveContentCategory] = Counter()
        macro_parts = {
            info.filename
            for info, _ in entries
            if posixpath.basename(info.filename).casefold() in _MACRO_NAMES
        }
        discarded = set(macro_parts)
        # Relationship parts belonging to deleted macro parts must not be orphaned.
        for name in macro_parts:
            discarded.add(ooxml_relationship_part(name))
        if macro_parts:
            removed[OoxmlActiveContentCategory.MACRO] = len(macro_parts)

        updated = []
        for info, data in entries:
            name = info.filename
            if name in discarded:
                continue
            if name.endswith(".rels"):
                root = ET.fromstring(data)
                if root.tag != f"{{{_RELS}}}Relationships":
                    raise ValueError("invalid relationships root")
                changed = False
                for relationship in list(root):
                    if relationship.tag != f"{{{_RELS}}}Relationship":
                        continue
                    target = relationship.get("Target", "")
                    if relationship.get("TargetMode", "").casefold() == "external":
                        kind = (
                            relationship.get("Type", "").rsplit("/", 1)[-1].casefold()
                        )
                        category = (
                            OoxmlActiveContentCategory.REMOTE_TEMPLATE
                            if kind == "attachedtemplate"
                            else OoxmlActiveContentCategory.REMOTE_REFERENCE
                        )
                        root.remove(relationship)
                        removed[category] += 1
                        changed = True
                    elif (
                        resolve_ooxml_target(name, target)
                        in discarded
                    ):
                        root.remove(relationship)
                        changed = True
                if changed:
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif name == "[Content_Types].xml" and macro_parts:
                changed = False
                for child in list(content_types):
                    if child.tag == f"{{{_CT}}}Override":
                        target = unquote(child.get("PartName", "")).lstrip("/")
                        if target in discarded:
                            content_types.remove(child)
                            changed = True
                if changed:
                    data = ET.tostring(
                        content_types, encoding="utf-8", xml_declaration=True
                    )
            updated.append((info, data))
        return updated, removed

    def _unsupported(self) -> OoxmlActiveContentReport:
        return self._failure(
            OoxmlActiveContentFinding.UNSUPPORTED_FORMAT,
            "Package is not a supported Word, Excel, or PowerPoint OOXML document.",
        )

    @staticmethod
    def _failure(
        code: OoxmlActiveContentFinding, message: str
    ) -> OoxmlActiveContentReport:
        return OoxmlActiveContentReport(
            outcome=OoxmlActiveContentOutcome.FAILED,
            issues=(OoxmlActiveContentIssue(code=code, message=message),),
        )
