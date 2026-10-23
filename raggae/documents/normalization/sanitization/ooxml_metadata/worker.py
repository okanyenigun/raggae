"""Remove standard core, application, and custom-property parts from OOXML."""

import copy
import posixpath
import zipfile
from typing import BinaryIO, Literal, Unpack, overload
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from .models import (
    OoxmlMetadataFinding,
    OoxmlMetadataIssue,
    OoxmlMetadataOutcome,
    OoxmlMetadataReport,
)
from ...io import SharedInputOutputHandler
from ...utils import ooxml_relationship_part, resolve_ooxml_target
from ...models import NormalizationRequest, NormalizationResult
from ...request import NormalizationArguments, resolve_request


_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_PROPERTY_PARTS: tuple[
    Literal["docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"], ...
] = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
_MAIN_PARTS: dict[str, Literal["docx", "xlsx", "pptx"]] = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}


class OoxmlMetadataSanitizer:
    """Opt-in metadata removal, without active-content or body-content cleanup."""

    def __init__(self) -> None:
        self._io_handler = SharedInputOutputHandler()

    @property
    def name(self) -> str:
        return "normalization_ooxml_metadata"

    @overload
    def normalize(
        self, request: NormalizationRequest
    ) -> NormalizationResult[OoxmlMetadataReport]: ...

    @overload
    def normalize(
        self, **kwargs: Unpack[NormalizationArguments]
    ) -> NormalizationResult[OoxmlMetadataReport]: ...

    def normalize(
        self,
        request: NormalizationRequest | None = None,
        **kwargs: object,
    ) -> NormalizationResult[OoxmlMetadataReport]:
        request = resolve_request(request, kwargs)
        report, artifact = self._io_handler.execute(
            source=request.source,
            output_path=request.output_path,
            transform=self._transform,
        )
        return NormalizationResult(artifact=artifact, report=report)

    def _transform(self, source: BinaryIO, output: BinaryIO) -> OoxmlMetadataReport:
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
            removed_parts: tuple[
                Literal["docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"],
                ...,
            ] = tuple(name for name in _PROPERTY_PARTS if name in payloads)
            updated = self._remove(entries, content_types, set(removed_parts))
        except (
            zipfile.BadZipFile,
            ET.ParseError,
            ValueError,
            RuntimeError,
            NotImplementedError,
        ):
            return self._failure(
                OoxmlMetadataFinding.DECODE_FAILED,
                "OOXML package could not be read strictly.",
            )
        if not removed_parts:
            return OoxmlMetadataReport(
                outcome=OoxmlMetadataOutcome.NO_CHANGE,
                document_format=document_format,
            )
        try:
            with zipfile.ZipFile(output, "w") as archive:
                archive.comment = comment
                for info, data in updated:
                    archive.writestr(copy.copy(info), data)
        except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError):
            return self._failure(
                OoxmlMetadataFinding.ENCODE_FAILED,
                "Sanitized OOXML package could not be encoded.",
            )
        return OoxmlMetadataReport(
            outcome=OoxmlMetadataOutcome.CHANGED,
            document_format=document_format,
            removed_parts=removed_parts,
        )

    @staticmethod
    def _remove(
        entries: list[tuple[zipfile.ZipInfo, bytes]],
        content_types: ET.Element,
        removed_parts: set[str],
    ) -> list[tuple[zipfile.ZipInfo, bytes]]:
        discarded = set(removed_parts)
        for name in removed_parts:
            discarded.add(ooxml_relationship_part(name))
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
                    if relationship.get("TargetMode", "").casefold() == "external":
                        continue
                    target = resolve_ooxml_target(
                        name,
                        relationship.get("Target", ""),
                    )
                    if target in discarded:
                        root.remove(relationship)
                        changed = True
                if changed:
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif name == "[Content_Types].xml" and removed_parts:
                changed = False
                for child in list(content_types):
                    if child.tag != f"{{{_CT}}}Override":
                        continue
                    target = posixpath.normpath(
                        unquote(child.get("PartName", "")).lstrip("/")
                    )
                    if target in discarded:
                        content_types.remove(child)
                        changed = True
                if changed:
                    data = ET.tostring(
                        content_types, encoding="utf-8", xml_declaration=True
                    )
            updated.append((info, data))
        return updated

    def _unsupported(self) -> OoxmlMetadataReport:
        return self._failure(
            OoxmlMetadataFinding.UNSUPPORTED_FORMAT,
            "Package is not a supported Word, Excel, or PowerPoint OOXML document.",
        )

    @staticmethod
    def _failure(code: OoxmlMetadataFinding, message: str) -> OoxmlMetadataReport:
        return OoxmlMetadataReport(
            outcome=OoxmlMetadataOutcome.FAILED,
            issues=(OoxmlMetadataIssue(code=code, message=message),),
        )
