"""Per-request targets, incremental encoders, BOMs, and strict output failure."""

import codecs
from dataclasses import FrozenInstanceError, replace
from io import BytesIO
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    HtmlMetadataSanitizer,
    NormalizationRequest,
    StrictTextEncodingNormalizer,
    TextEncodingFinding,
    TextEncodingReport,
)
from raggae.documents.normalization.models import TextEncoding

TEXT = "Café €\r\n日本語\n"


def test_request_type_and_validation_share_exactly_the_approved_targets():
    assert get_args(TextEncoding) == (
        "utf-8", "utf-8-sig", "utf-16", "utf-32",
        "cp1252", "iso-8859-1", "ascii",
    )
    for annotations in (get_type_hints(NormalizationRequest), get_type_hints(NormalizationRequest.__init__)):
        assert annotations["target_encoding"] == TextEncoding
        assert annotations["declared_encoding"] == TextEncoding | None


def test_shared_request_default_trim_immutability_and_replace():
    default = NormalizationRequest(source=b"hello", output_path=None)
    assert default.target_encoding == "utf-8"
    assert default.declared_encoding is None
    request = NormalizationRequest(source=b"hello", output_path=None, target_encoding=" utf-16 ")
    assert request.target_encoding == "utf-16"
    advanced = replace(request, source=Artifact(data=b"next"))
    assert advanced.target_encoding == "utf-16"
    with pytest.raises(FrozenInstanceError):
        request.target_encoding = "ascii"


@pytest.mark.parametrize("target,error", [(None, TypeError), (True, TypeError), (1, TypeError), (b"utf-8", TypeError), ("", ValueError), (" \t", ValueError)])
def test_invalid_request_targets_are_sdk_errors(target, error):
    with pytest.raises(error, match="target_encoding"):
        NormalizationRequest(source=b"hello", output_path=None, target_encoding=target)


@pytest.mark.parametrize("encoding,text", [
    ("utf-8", TEXT), ("utf-8-sig", TEXT), ("utf-16", TEXT), ("utf-32", TEXT),
    ("cp1252", "Café €\r\n"), ("iso-8859-1", "Café\r\n"),
    ("ascii", "Plain text\r\n"),
])
@pytest.mark.parametrize("disk", [False, True])
def test_all_approved_source_encodings_work_through_public_request(encoding, text, disk, tmp_path):
    original = text.encode(encoding)
    destination = tmp_path / "normalized.txt"
    request = NormalizationRequest(
        source=original, output_path=destination if disk else None,
        declared_encoding=encoding,
    )
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(request)
    assert result.artifact.read_bytes() == text.encode("utf-8")
    assert request.source.read_bytes() == original
    expected = "no_change" if encoding in ("utf-8", "ascii") else "changed"
    assert result.outcome == expected
    assert destination.exists() is (disk and expected == "changed")
    assert not list(tmp_path.glob(".raggae-*.part"))


@pytest.mark.parametrize("declaration", [
    "not-an-encoding", "base64_codec", "hex_codec", "zlib_codec", "rot_13",
    "UTF8", "utf_8", "latin-1", "iso8859-1", "windows-1252", "UTF-16",
    "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "iso2022_jp",
    "shift_jis", "koi8-r",
])
def test_unlisted_source_encodings_are_rejected_before_io(declaration, tmp_path):
    with pytest.raises(ValueError, match="declared_encoding must be one of"):
        NormalizationRequest(
            source=tmp_path / "missing.txt", output_path=tmp_path / "never-written.txt",
            declared_encoding=declaration,
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target,text", [
    ("utf-8", TEXT), ("utf-8-sig", TEXT), ("utf-16", TEXT), ("utf-32", TEXT),
    ("cp1252", "Café €\r\n"), ("iso-8859-1", "Café\r\n"),
    ("ascii", "Plain text\r\n"),
])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 65536])
@pytest.mark.parametrize("disk", [False, True])
def test_utf8_can_be_converted_to_a_requested_target(target, text, chunk_size, disk, tmp_path):
    original = text.encode("utf-8")
    request = NormalizationRequest(
        source=original, output_path=tmp_path / "result.txt" if disk else None,
        target_encoding=target,
    )
    result = StrictTextEncodingNormalizer(chunk_size=chunk_size).normalize(request)
    assert result.outcome == ("no_change" if target == "utf-8" else "changed")
    assert result.artifact.read_bytes() == text.encode(target)
    assert result.artifact.read_bytes().decode(target) == text
    assert result.report.source_encoding == "utf-8"
    assert result.report.target_encoding == codecs.lookup(target).name
    assert not result.report.bom_removed
    assert request.source.read_bytes() == original
    assert not list(tmp_path.glob(".raggae-*.part"))
    if disk:
        assert (tmp_path / "result.txt").exists() is (target != "utf-8")
    assert TextEncodingReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("source_kind", ["path", "bytes", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_source_and_target_belong_to_the_request(source_kind, disk, tmp_path):
    text = "Café €12\r\n"
    original = text.encode("cp1252")
    source = original
    if source_kind == "path":
        source = tmp_path / "original.csv"
        source.write_bytes(original)
    elif source_kind == "artifact":
        source = Artifact(data=original)
    request = NormalizationRequest(source=source, output_path=tmp_path / "target.csv" if disk else None, declared_encoding="cp1252", target_encoding="utf-16")
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(request)
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == text.encode("utf-16")
    assert result.report.source_encoding == "cp1252"
    assert result.report.target_encoding == "utf-16"
    assert request.source.read_bytes() == original


@pytest.mark.parametrize("target,text", [("cp1252", "Café €"), ("iso-8859-1", "Café"), ("ascii", "Plain text")])
def test_legacy_targets(target, text):
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=text.encode("utf-8"), output_path=None, target_encoding=target))
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == text.encode(target)
    assert result.report.target_encoding == codecs.lookup(target).name


@pytest.mark.parametrize("target", ["utf-8", " utf-8 "])
def test_utf8_preserves_no_change(target):
    source = Artifact(data=TEXT.encode("utf-8"))
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(source=source, output_path=None, target_encoding=target))
    assert result.outcome == "no_change"
    assert result.artifact is source
    assert result.report.target_encoding == "utf-8"


@pytest.mark.parametrize("encoding,text,declaration", [
    ("cp1252", "Café €", "cp1252"),
    ("iso-8859-1", "Café", "iso-8859-1"),
    ("utf-16", TEXT, None), ("utf-32", TEXT, None), ("utf-8-sig", TEXT, None),
])
def test_existing_target_returns_original_without_writing(encoding, text, declaration, tmp_path):
    source = Artifact(data=text.encode(encoding))
    destination = tmp_path / "not-created.txt"
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=source, output_path=destination, declared_encoding=declaration, target_encoding=encoding))
    assert result.outcome == "no_change"
    assert result.artifact is source
    assert not result.report.bom_removed
    assert result.report.target_encoding == codecs.lookup(encoding).name
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target", ["utf-16", "utf-32", "utf-8-sig"])
def test_idempotent_when_target_bom_already_matches(target):
    worker = StrictTextEncodingNormalizer(chunk_size=1)
    first = worker.normalize(NormalizationRequest(source=TEXT.encode("utf-8"), output_path=None, target_encoding=target))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None, target_encoding=target))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact


@pytest.mark.parametrize("source_encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_explicit_endian_source_with_marker_can_be_read(source_encoding):
    marker = {"utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE, "utf-32-le": codecs.BOM_UTF32_LE, "utf-32-be": codecs.BOM_UTF32_BE}[source_encoding]
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=marker + TEXT.encode(source_encoding), output_path=None))
    assert result.outcome == "changed"
    assert result.report.bom_removed
    assert result.artifact.read_bytes() == TEXT.encode("utf-8")


def test_non_native_source_bom_is_reencoded_for_generic_utf16_target():
    endian = "utf-16-be" if codecs.BOM_UTF16 == codecs.BOM_UTF16_LE else "utf-16-le"
    marker = codecs.BOM_UTF16_BE if endian == "utf-16-be" else codecs.BOM_UTF16_LE
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=marker + TEXT.encode(endian), output_path=None, target_encoding="utf-16"))
    assert result.outcome == "changed"
    assert result.report.bom_removed
    assert result.artifact.read_bytes() == TEXT.encode("utf-16")


@pytest.mark.parametrize("target", ["utf-16", "utf-32", "utf-8-sig", "ascii"])
def test_empty_text_uses_target_codec_preamble(target):
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(source=b"", output_path=None, target_encoding=target))
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == "".encode(target)


@pytest.mark.parametrize("target,text", [("ascii", "PRIVATE café"), ("cp1252", "PRIVATE 日本語"), ("iso-8859-1", "PRIVATE €")])
@pytest.mark.parametrize("disk", [False, True])
def test_unrepresentable_characters_fail_without_replacement_or_artifact(target, text, disk, tmp_path):
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=text.encode("utf-8"), output_path=tmp_path / "failed.txt" if disk else None, target_encoding=target))
    assert result.outcome == "failed" and result.artifact is None
    assert result.report.issues[0].code is TextEncodingFinding.ENCODE_FAILED
    assert result.report.target_encoding == codecs.lookup(target).name
    assert "PRIVATE" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target", [
    "not-an-encoding", "base64_codec", "hex_codec", "zlib_codec", "rot_13",
    "UTF8", "utf_8", "latin-1", "iso8859-1", "windows-1252", "UTF-16",
    "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "iso2022_jp",
])
def test_unlisted_targets_are_rejected_when_request_is_created(target, tmp_path):
    with pytest.raises(ValueError, match="target_encoding must be one of"):
        NormalizationRequest(source=b"hello", output_path=tmp_path / "failed.txt", target_encoding=target)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source,target,declaration", [
    (b"caf\xe9", "utf-16", None),
    (codecs.BOM_UTF16 + b"x", "utf-16", None),
    (b"caf\xe9", "ascii", "ascii"),
])
def test_source_failures_still_record_requested_target(source, target, declaration):
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(source=source, output_path=None, declared_encoding=declaration, target_encoding=target))
    assert result.outcome == "failed" and result.artifact is None
    assert result.report.target_encoding == target
    assert result.report.issues[0].code in {TextEncodingFinding.ENCODING_REQUIRED, TextEncodingFinding.DECODE_FAILED}


def test_same_legacy_encoding_still_validates_all_source_bytes():
    result = StrictTextEncodingNormalizer(chunk_size=1).normalize(NormalizationRequest(source=b"caf\xe9\x81", output_path=None, declared_encoding="cp1252", target_encoding="cp1252"))
    assert result.outcome == "failed" and result.artifact is None
    assert result.report.issues[0].code is TextEncodingFinding.DECODE_FAILED


def test_target_does_not_act_as_source_encoding_evidence():
    result = StrictTextEncodingNormalizer().normalize(NormalizationRequest(source=b"caf\xe9", output_path=None, target_encoding="cp1252"))
    assert result.outcome == "failed"
    assert result.report.issues[0].code is TextEncodingFinding.ENCODING_REQUIRED


def test_single_worker_obeys_each_request_independently():
    worker = StrictTextEncodingNormalizer(chunk_size=1)
    for target in ("utf-16", "cp1252", "utf-8"):
        result = worker.normalize(NormalizationRequest(source="Café".encode("utf-8"), output_path=None, target_encoding=target))
        assert result.artifact.read_bytes() == "Café".encode(target)
        assert result.report.target_encoding == codecs.lookup(target).name


def test_other_workers_ignore_valid_target_encoding():
    result = HtmlMetadataSanitizer().normalize(NormalizationRequest(source='<meta name="author" content="Jane"><p>Café</p>'.encode("utf-8"), output_path=None, target_encoding="ascii"))
    assert result.outcome == "changed"
    assert result.artifact.read_bytes() == "<p>Café</p>".encode("utf-8")


def test_no_change_with_legacy_target_never_calls_output_write():
    class Output(BytesIO):
        def write(self, data):
            pytest.fail("a no-change operation must not write output")

    with BytesIO("Café".encode("cp1252")) as source, Output() as output:
        report = StrictTextEncodingNormalizer()._transform(source, output, declared_encoding="cp1252", target_encoding="cp1252")
    assert report.outcome == "no_change"


@pytest.mark.parametrize("target", ["utf-16", "utf-32", "utf-8-sig"])
def test_unicode_target_supports_short_destination_writes(target):
    class Output(BytesIO):
        def write(self, data):
            return super().write(data[:1])

    text = "日本語\nASCII"
    with BytesIO(text.encode("utf-8")) as source, Output() as output:
        report = StrictTextEncodingNormalizer(chunk_size=1)._transform(source, output, target_encoding=target)
        assert report.outcome == "changed"
        assert output.getvalue() == text.encode(target)


@pytest.mark.parametrize("target", [None, True, "", " "])
def test_report_rejects_invalid_target_names(target):
    with pytest.raises(ValidationError):
        TextEncodingReport(outcome="changed", source_encoding="utf-8", target_encoding=target)


def test_legacy_no_change_report_can_round_trip():
    report = TextEncodingReport(outcome="no_change", source_encoding="cp1252", target_encoding="cp1252")
    assert TextEncodingReport.model_validate_json(report.model_dump_json()) == report
