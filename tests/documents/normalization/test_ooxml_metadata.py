"""Standard property removal, reference cleanup, and unrelated-part preservation."""

from io import BytesIO
import zipfile
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    NormalizationResult,
    OoxmlMetadataFinding,
    OoxmlMetadataIssue,
    NormalizationWorker,
    OoxmlMetadataReport,
    OoxmlMetadataSanitizer,
)

CT = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
MAIN = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
PROPERTIES = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")


def relationships(*entries):
    root = ET.Element(f"{{{RELS}}}Relationships")
    for entry in entries:
        ET.SubElement(root, f"{{{RELS}}}Relationship", entry)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parts(family="docx", properties=PROPERTIES):
    main = MAIN[family]
    parent = main.split("/")[0]
    root = ET.Element(f"{{{CT}}}Types")
    ET.SubElement(root, f"{{{CT}}}Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(root, f"{{{CT}}}Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(root, f"{{{CT}}}Override", PartName="/" + main, ContentType="application/xml")
    payloads = {
        main: b"<document>Keep document text</document>",
        parent + "/media/image.bin": b"KEEP_IMAGE",
        parent + "/vbaProject.bin": b"KEEP_MACRO",
        "docProps/vendor.xml": b"<vendor>Keep vendor metadata</vendor>",
        "_xmlsignatures/sig1.xml": b"KEEP_SIGNATURE_PART",
    }
    entries = [
        {"Id": "main", "Type": OFFICE + "officeDocument", "Target": main},
        {"Id": "remote", "Type": OFFICE + "hyperlink", "Target": "https://example.invalid/docProps/core.xml", "TargetMode": "External"},
        {"Id": "vendor", "Type": OFFICE + "customProperties", "Target": "docProps/vendor.xml"},
    ]
    for name in properties:
        payloads[name] = b"<properties>Private author/title/custom value</properties>"
        ET.SubElement(root, f"{{{CT}}}Override", PartName="/" + name, ContentType="application/xml")
        entries.append({"Id": name, "Type": OFFICE + "properties", "Target": name})
    payloads["_rels/.rels"] = relationships(*entries)
    payloads["[Content_Types].xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if family == "xlsx":
        payloads["xl/worksheets/sheet1.xml"] = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>SUM(B1:B2)</f><v>3</v></c></row></sheetData></worksheet>'
    if family == "pptx":
        payloads["ppt/slides/slide1.xml"] = b"<slide>Keep slide content</slide>"
    return payloads


def package(payloads, *, compression=zipfile.ZIP_DEFLATED):
    with BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            archive.comment = b"Keep ZIP comment"
            for name, data in payloads.items():
                info = zipfile.ZipInfo(name, date_time=(2024, 2, 3, 4, 5, 6))
                info.compress_type = compression
                info.external_attr = 0o100644 << 16
                info.comment = b"Keep entry comment"
                archive.writestr(info, data)
        return output.getvalue()


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_properties_and_references_removed_without_changing_other_parts(family, source_kind, disk, tmp_path):
    payloads = parts(family)
    original = package(payloads)
    source = original
    if source_kind == "path":
        path = tmp_path / "original.bin"
        path.write_bytes(original)
        source = str(path)
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.bin" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = OoxmlMetadataSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.document_format == family
    assert result.report.removed_parts == PROPERTIES
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert archive.testzip() is None
        assert archive.comment == b"Keep ZIP comment"
        for name, data in payloads.items():
            if name in PROPERTIES:
                assert name not in archive.namelist()
            elif name not in {"[Content_Types].xml", "_rels/.rels"}:
                assert archive.read(name) == data
        root = ET.fromstring(archive.read("_rels/.rels"))
        assert [item.get("Id") for item in root] == ["main", "remote", "vendor"]
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        assert all(item.get("PartName", "").lstrip("/") not in PROPERTIES for item in types)
        info = archive.getinfo(MAIN[family])
        assert info.date_time == (2024, 2, 3, 4, 5, 6)
        assert info.external_attr == 0o100644 << 16
        assert info.comment == b"Keep entry comment"
    assert "Private" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert OoxmlMetadataReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("properties", [
    (PROPERTIES[0],), (PROPERTIES[1],), (PROPERTIES[2],),
    PROPERTIES[:2], PROPERTIES[1:], (PROPERTIES[0], PROPERTIES[2]),
])
def test_each_subset_reported(properties):
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(parts(properties=properties)), output_path=None))
    assert result.report.removed_parts == properties


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
def test_no_standard_properties_returns_exact_original(family, tmp_path):
    artifact = Artifact(data=package(parts(family, properties=())))
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=artifact, output_path=tmp_path / "unused"))
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert result.report.removed_parts == ()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target", ["../docProps/core.xml", "/docProps/core.xml", "../docProps/./core.xml", "../docProps/core%2Exml"])
def test_relative_absolute_and_encoded_relationship_targets(target):
    payloads = parts()
    payloads["word/_rels/settings.xml.rels"] = relationships(
        {"Id": "remove", "Type": OFFICE + "properties", "Target": target},
        {"Id": "keep", "Type": OFFICE + "properties", "Target": "vendor/docProps/core.xml"},
    )
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        root = ET.fromstring(archive.read("word/_rels/settings.xml.rels"))
        assert [item.get("Id") for item in root] == ["keep"]


def test_property_part_relationships_and_encoded_overrides_removed():
    payloads = parts()
    payloads["docProps/_rels/core.xml.rels"] = relationships({"Id": "own", "Type": OFFICE + "hyperlink", "Target": "https://example.invalid", "TargetMode": "External"})
    root = ET.fromstring(payloads["[Content_Types].xml"])
    for item in root:
        if item.get("PartName") == "/docProps/core.xml":
            item.set("PartName", "/docProps/core%2Exml")
    ET.SubElement(root, f"{{{CT}}}Override", PartName="/docProps/_rels/core.xml.rels", ContentType="application/xml")
    payloads["[Content_Types].xml"] = ET.tostring(root)
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    with zipfile.ZipFile(BytesIO(result.artifact.read_bytes())) as archive:
        assert "docProps/_rels/core.xml.rels" not in archive.namelist()
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        assert all("core" not in item.get("PartName", "") for item in types)


def test_property_payload_not_parsed_when_whole_part_is_removed():
    payloads = parts(properties=(PROPERTIES[0],))
    payloads[PROPERTIES[0]] = b"broken property XML"
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.outcome == "changed"
    assert result.report.removed_parts == (PROPERTIES[0],)


def test_idempotence_and_shared_settings_ignored():
    worker = OoxmlMetadataSanitizer()
    first = worker.normalize(NormalizationRequest(source=package(parts()), output_path=None, password="unused-secret", declared_encoding="ascii"))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact
    assert "unused-secret" not in repr(first)


@pytest.mark.parametrize("data", [b"", b"not a ZIP", b"PK\x03\x04invalid"])
def test_invalid_zip_failed_without_output(data, tmp_path):
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output"))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlMetadataFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payloads", [{"readme.txt": b"hello"}, {"[Content_Types].xml": f'<Types xmlns="{CT}"/>'.encode()}])
def test_unsupported_zip_is_not_document(payloads):
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlMetadataFinding.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("name,data", [
    ("[Content_Types].xml", b"broken XML"), ("[Content_Types].xml", b"<WrongRoot/>"),
    ("_rels/.rels", b"broken XML"), ("_rels/.rels", b"<WrongRoot/>"),
])
def test_malformed_control_xml_is_decode_failure(name, data, tmp_path):
    payloads = parts()
    payloads[name] = data
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(payloads), output_path=tmp_path / "output"))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlMetadataFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_duplicate_parts_rejected():
    with BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in parts().items():
                archive.writestr(name, data)
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(PROPERTIES[0], b"second copy")
        data = output.getvalue()
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlMetadataFinding.DECODE_FAILED


def test_crc_checked_even_for_removed_metadata():
    payloads = parts()
    payloads[PROPERTIES[0]] = b"MUST_CHECK_CRC"
    data = package(payloads, compression=zipfile.ZIP_STORED)
    index = data.index(b"MUST_CHECK_CRC")
    data = data[:index] + b"X" + data[index + 1:]
    result = OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == OoxmlMetadataFinding.DECODE_FAILED


@pytest.mark.parametrize("error", [ValueError("private encoder error"), OSError("disk full")])
def test_partial_output_failure_cleanup(error, monkeypatch, tmp_path):
    data = package(parts())

    def broken_write(self, *args, **kwargs):
        self.fp.write(b"partial output")
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "writestr", broken_write)
    request = NormalizationRequest(source=data, output_path=tmp_path / "output")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            OoxmlMetadataSanitizer().normalize(request)
        assert caught.value is error
    else:
        result = OoxmlMetadataSanitizer().normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == OoxmlMetadataFinding.ENCODE_FAILED
        assert result.report.removed_parts == ()
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_existing_destination_preserved(tmp_path):
    path = tmp_path / "existing"
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        OoxmlMetadataSanitizer().normalize(NormalizationRequest(source=package(parts()), output_path=path))
    assert path.read_bytes() == b"keep"


def test_worker_contract():
    worker = OoxmlMetadataSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_ooxml_metadata"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})


def test_report_read_only_and_serializable():
    report = OoxmlMetadataReport(outcome="changed", document_format="docx", removed_parts=[PROPERTIES[0]])
    assert report.removed_parts == (PROPERTIES[0],)
    assert OoxmlMetadataReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.removed_parts = ()
    with pytest.raises(ValidationError):
        OoxmlMetadataIssue(code=OoxmlMetadataFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "no_change"},
    {"outcome": "changed", "document_format": "docx"},
    {"outcome": "no_change", "document_format": "docx", "removed_parts": [PROPERTIES[0]]},
    {"outcome": "changed", "document_format": "docx", "removed_parts": [PROPERTIES[0], PROPERTIES[0]]},
    {"outcome": "changed", "document_format": "docx", "removed_parts": ["docProps/vendor.xml"]},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        OoxmlMetadataReport(**kwargs)
