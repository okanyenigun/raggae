"""OOXML package parts, relationship resolution, preservation, and cleanup."""

from io import BytesIO
from xml.etree import ElementTree as ET
import zipfile

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    OoxmlActiveContentFinding,
    OoxmlActiveContentIssue,
    NormalizationWorker,
    OoxmlActiveContentReport,
    OoxmlActiveContentSanitizer,
)

CT = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
MAIN = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}


def relationships(*entries):
    root = ET.Element(f"{{{RELS}}}Relationships")
    for entry in entries:
        ET.SubElement(root, f"{{{RELS}}}Relationship", entry)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parts(family="docx", active=False):
    main = MAIN[family]
    parent, filename = main.split("/")
    types = ET.Element(f"{{{CT}}}Types")
    ET.SubElement(types, f"{{{CT}}}Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(types, f"{{{CT}}}Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, f"{{{CT}}}Override", PartName="/" + main, ContentType="application/xml")
    result = {
        main: b"<document><text>Keep this text</text></document>",
        "docProps/core.xml": b"<properties>Keep this author</properties>",
        parent + "/media/image.bin": b"KEEP_IMAGE_BYTES",
        parent + "/styles.xml": b"<styles/>",
        "_rels/.rels": relationships({"Id": "main", "Type": OFFICE + "officeDocument", "Target": main}),
    }
    if family == "xlsx":
        result["xl/worksheets/sheet1.xml"] = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>SUM(B1:B2)</f><v>3</v></c></row></sheetData></worksheet>'
    if family == "pptx":
        result["ppt/slides/slide1.xml"] = b"<slide>Keep this slide</slide>"
    entries = [{"Id": "styles", "Type": OFFICE + "styles", "Target": "styles.xml"}]
    if active:
        for macro in ["vbaProject.bin", "vbaData.xml", "vbaProjectSignature.bin"]:
            path = parent + "/" + macro
            result[path] = b"MACRO_PAYLOAD"
            ET.SubElement(types, f"{{{CT}}}Override", PartName="/" + path, ContentType="application/vnd.ms-office.vbaProject")
            entries.append({"Id": macro, "Type": OFFICE + "vbaProject", "Target": macro})
        result[parent + "/_rels/vbaProject.bin.rels"] = relationships({
            "Id": "sig", "Type": OFFICE + "vbaProjectSignature", "Target": "vbaProjectSignature.bin",
        })
        entries.extend([
            {"Id": "remote", "Type": OFFICE + "hyperlink", "Target": "https://example.invalid/resource", "TargetMode": "External"},
            {"Id": "template", "Type": OFFICE + "attachedTemplate", "Target": "https://example.invalid/template.dotm", "TargetMode": "External"},
        ])
    result[parent + "/_rels/" + filename + ".rels"] = relationships(*entries)
    result["[Content_Types].xml"] = ET.tostring(types, encoding="utf-8", xml_declaration=True)
    return result


def package(payloads, *, compression=zipfile.ZIP_DEFLATED):
    with BytesIO() as output:
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            archive.comment = b"Keep ZIP comment"
            for name, data in payloads.items():
                info = zipfile.ZipInfo(name, date_time=(2024, 2, 3, 4, 5, 6))
                info.compress_type = compression
                info.external_attr = 0o100644 << 16
                info.comment = b"Keep entry comment"
                archive.writestr(info, data)
        return output.getvalue()


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
def test_removal_and_ordinary_content_preservation(family, disk, source_kind, tmp_path):
    payloads = parts(family, active=True)
    original = package(payloads)
    source = original
    if source_kind == "path":
        path = tmp_path / "original.bin"
        path.write_bytes(original)
        source = path
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.bin" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = OoxmlActiveContentSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.document_format == family
    assert result.report.removals == {"macro": 3, "remote_reference": 1, "remote_template": 1}
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    parent, filename = MAIN[family].split("/")
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert archive.testzip() is None
        assert archive.comment == b"Keep ZIP comment"
        for name, data in payloads.items():
            if name.endswith(("vbaProject.bin", "vbaData.xml", "vbaProjectSignature.bin", "vbaProject.bin.rels")):
                assert name not in archive.namelist()
            elif name == "[Content_Types].xml" or name == parent + "/_rels/" + filename + ".rels":
                continue
            else:
                assert archive.read(name) == data
        relation_root = ET.fromstring(archive.read(parent + "/_rels/" + filename + ".rels"))
        assert [child.get("Id") for child in relation_root] == ["styles"]
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        assert all("vba" not in child.get("PartName", "") for child in types)
        info = archive.getinfo(MAIN[family])
        assert info.date_time == (2024, 2, 3, 4, 5, 6)
        assert info.external_attr == 0o100644 << 16
        assert info.comment == b"Keep entry comment"
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert OoxmlActiveContentReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
def test_no_changes_returns_exact_original(family, tmp_path):
    artifact = Artifact(data=package(parts(family)))
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(
        source=artifact, output_path=tmp_path / "unused",
    ))
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target", ["vbaProject.bin", "./vbaProject.bin", "../word/vbaProject.bin", "/word/vbaProject.bin", "vbaProject%2Ebin"])
def test_macro_relationship_resolution(target):
    payloads = parts(active=True)
    payloads["word/_rels/document.xml.rels"] = relationships(
        {"Id": "macro", "Type": OFFICE + "vbaProject", "Target": target},
        {"Id": "keep", "Type": OFFICE + "image", "Target": "media/vbaProject.bin"},
    )
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        # Matching the basename alone would accidentally remove this different part.
        assert [item.get("Id") for item in root] == ["keep"]


@pytest.mark.parametrize("mode", ["External", "external", "EXTERNAL"])
def test_external_relationships_removed_without_macros(mode):
    payloads = parts()
    payloads["word/_rels/document.xml.rels"] = relationships({
        "Id": "remote", "Type": OFFICE + "hyperlink", "Target": "https://example.invalid", "TargetMode": mode,
    })
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.report.removals == {"remote_reference": 1}


def test_root_relationships_and_nested_relationships_are_processed():
    payloads = parts()
    payloads["_rels/.rels"] = relationships(
        {"Id": "main", "Type": OFFICE + "officeDocument", "Target": "word/document.xml"},
        {"Id": "remote", "Type": OFFICE + "hyperlink", "Target": "https://example.invalid", "TargetMode": "External"},
    )
    payloads["word/_rels/settings.xml.rels"] = relationships({
        "Id": "template", "Type": OFFICE + "attachedTemplate", "Target": "https://example.invalid", "TargetMode": "External",
    })
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.report.removals == {"remote_reference": 1, "remote_template": 1}


def test_only_exact_macro_part_names_removed():
    payloads = parts()
    payloads["word/myvbaProject.bin"] = b"KEEP"
    payloads["word/VBAPROJECT.BIN"] = b"REMOVE"
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.report.removals == {"macro": 1}
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert archive.read("word/myvbaProject.bin") == b"KEEP"
        assert "word/VBAPROJECT.BIN" not in archive.namelist()


def test_idempotent_and_shared_settings_ignored():
    worker = OoxmlActiveContentSanitizer()
    first = worker.normalize(NormalizationRequest(
        source=Artifact(data=package(parts(active=True)), password="unused-secret"), output_path=None,  declared_encoding="ascii",
    ))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


@pytest.mark.parametrize("data", [b"", b"not a ZIP", b"PK\x03\x04invalid"])
def test_invalid_zip(data, tmp_path):
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output"))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlActiveContentFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payloads", [{"readme.txt": b"hello"}, {"[Content_Types].xml": f'<Types xmlns="{CT}"/>'.encode()}])
def test_non_document_zip_rejected(payloads):
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlActiveContentFinding.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("name,value", [
    ("[Content_Types].xml", b"invalid XML"),
    ("[Content_Types].xml", b"<WrongRoot/>"),
    ("word/_rels/document.xml.rels", b"invalid XML"),
    ("word/_rels/document.xml.rels", b"<WrongRoot/>"),
])
def test_malformed_control_xml(name, value, tmp_path):
    payloads = parts(active=True)
    payloads[name] = value
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=tmp_path / "output"))
    assert result.outcome == "failed"
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlActiveContentFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_duplicate_zip_parts_rejected():
    with BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in parts().items():
                archive.writestr(name, data)
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("word/document.xml", b"second copy")
        data = output.getvalue()
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=data, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlActiveContentFinding.DECODE_FAILED


def test_crc_checked_for_ordinary_parts_too():
    payloads = parts(active=True)
    payloads["word/keep.bin"] = b"MUST_CHECK_CRC"
    data = package(payloads, compression=zipfile.ZIP_STORED)
    index = data.index(b"MUST_CHECK_CRC")
    corrupted = data[:index] + b"X" + data[index + 1:]
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=corrupted, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlActiveContentFinding.DECODE_FAILED


@pytest.mark.parametrize("error", [ValueError("private encoder error"), OSError("disk full")])
def test_failed_output_cleanup(error, monkeypatch, tmp_path):
    data = package(parts(active=True))

    def broken_write(self, *args, **kwargs):
        self.fp.write(b"partial output")
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "writestr", broken_write)
    request = NormalizationRequest(source=data, output_path=tmp_path / "output")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            OoxmlActiveContentSanitizer().normalize(request)
        assert caught.value is error
    else:
        result = OoxmlActiveContentSanitizer().normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == OoxmlActiveContentFinding.ENCODE_FAILED
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_existing_destination_preserved(tmp_path):
    destination = tmp_path / "existing"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(parts(active=True)), output_path=destination))
    assert destination.read_bytes() == b"keep"


def test_signature_parts_not_deleted_to_hide_rewrite():
    payloads = parts(active=True)
    payloads["_xmlsignatures/sig1.xml"] = b"KEEP_SIGNATURE_PART"
    result = OoxmlActiveContentSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert archive.read("_xmlsignatures/sig1.xml") == b"KEEP_SIGNATURE_PART"


def test_worker_contract():
    worker = OoxmlActiveContentSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_ooxml_active_content"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


@pytest.mark.parametrize("removals", [{"macro": 0}, {"macro": True}, {"macro": "1"}, {"unknown": 1}])
def test_invalid_counts_rejected(removals):
    with pytest.raises(ValidationError):
        OoxmlActiveContentReport(outcome="changed", document_format="docx", removals=removals)


def test_report_counts_immutable_and_json_serializable():
    counts = {"macro": 1}
    report = OoxmlActiveContentReport(outcome="changed", document_format="xlsx", removals=counts)
    counts["macro"] = 99
    assert report.removals == {"macro": 1}
    with pytest.raises(TypeError):
        report.removals["macro"] = 2
    with pytest.raises(ValidationError):
        report.document_format = "pptx"
    assert OoxmlActiveContentReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        OoxmlActiveContentIssue(code=OoxmlActiveContentFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "no_change"},
    {"outcome": "changed", "document_format": "docx"},
    {"outcome": "no_change", "document_format": "docx", "removals": {"macro": 1}},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        OoxmlActiveContentReport(**kwargs)
