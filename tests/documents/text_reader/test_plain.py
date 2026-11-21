"""Complete source decoding, explicit encoding, and selection rejection."""

from io import BytesIO
from pathlib import Path
from stat import S_IFREG
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    PlainTextReader,
    TextReader,
    TextReadResult,
)


@pytest.fixture
def layout():
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=300,
                height=400,
            ),
        ),
        regions=(
            LayoutRegion(
                id="first", surface_id="page-1", kind="text", bbox=(10, 10, 100, 40)
            ),
            LayoutRegion(
                id="second", surface_id="page-1", kind="text", bbox=(10, 50, 100, 80)
            ),
        ),
    )


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_supported_inputs_preserve_text_and_source(tmp_path, source_kind, keyword):
    expected = "  Café\r\nİstanbul\n世界 🌍\rLast\tline  "
    data = expected.encode("utf-8")
    path = tmp_path / "notes.txt"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    worker = PlainTextReader()
    result = worker.read(source=source) if keyword else worker.read(source)

    assert result.outcome == "success"
    assert result.reader == worker.name == "text_reader_plain"
    (block,) = result.blocks
    assert block.text == expected
    assert block.page_index == 0
    assert "location" not in block.model_dump()
    assert block.region_id is None
    assert block.content_format == "plain_text" and block.method == "native"
    assert result.findings == ()
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data
    assert list(tmp_path.iterdir()) == [path]
    assert "private-password" not in repr(result)
    assert "private-password" not in result.model_dump_json()
    if isinstance(source, Artifact):
        assert source.password == "private-password"


def test_worker_implements_common_protocol_and_uses_explicit_encoding():
    worker = PlainTextReader(encoding="cp1252")
    assert isinstance(worker, TextReader)
    assert worker.read(b"Caf\xe9").blocks[0].text == "Café"
    assert PlainTextReader().read("Café".encode("utf-8")).blocks[0].text == "Café"


@pytest.mark.parametrize(
    "encoding,text",
    [
        ("utf-8", "Café 🌍"),
        ("utf-8-sig", "Café 🌍"),
        ("utf-16", "Café 🌍"),
        ("utf-32", "Café 🌍"),
        ("cp1252", "Café €"),
        ("iso-8859-1", "Café"),
        ("ascii", "Plain text"),
    ],
)
def test_explicit_source_encoding(encoding, text):
    result = PlainTextReader(encoding=encoding).read(text.encode(encoding))
    assert result.outcome == "success"
    assert result.blocks[0].text == text


def test_utf8_bom_is_not_silently_removed():
    data = "Café".encode("utf-8-sig")
    assert PlainTextReader().read(data).blocks[0].text == "\ufeffCafé"
    worker = PlainTextReader(encoding="utf-8-sig")
    assert worker.read(data).blocks[0].text == "Café"


@pytest.mark.parametrize("data", [b"Caf\xe9", b"\xff", b"ok\xf0\x9f"])
def test_invalid_encoding_is_not_guessed_replaced_or_partially_returned(data):
    result = PlainTextReader().read(data)
    assert result.outcome == "failed" and result.blocks == ()
    assert result.findings[0].code == "decode_failed"
    assert (
        result.findings[0].message
        == "Source could not be decoded using the configured encoding."
    )


def test_invalid_bytes_after_a_valid_prefix_fail_without_partial_text():
    result = PlainTextReader().read(b"valid prefix\xff")
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "decode_failed"


@pytest.mark.parametrize(
    "data,encoding", [(b"", "utf-8"), (b"\xef\xbb\xbf", "utf-8-sig")]
)
def test_empty_decoded_source_is_successful_and_explicit(data, encoding):
    result = PlainTextReader(encoding=encoding).read(data)
    assert result.outcome == "success" and result.blocks == ()
    (finding,) = result.findings
    assert finding.code == "empty"
    assert finding.page_index == 0


@pytest.mark.parametrize("text", [" ", "\r\n\n\t", "\x00", "**literal**\n# heading"])
def test_decoder_does_not_strip_or_interpret_source_characters(text):
    result = PlainTextReader().read(text.encode())
    assert result.outcome == "success"
    assert result.blocks[0].text == text


def test_file_extension_does_not_change_explicit_decoder_choice(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"raw source text")
    assert PlainTextReader().read(path).blocks[0].text == "raw source text"


@pytest.mark.parametrize("file_backed", [False, True])
def test_complete_input_and_output_beyond_the_former_limits(tmp_path, file_backed):
    text = "x" * (10 * 1024 * 1024) + " 🌍\r\n"
    data = text.encode("utf-8")
    source = data
    if file_backed:
        source = tmp_path / "source.txt"
        source.write_bytes(data)
    result = PlainTextReader().read(source)
    assert result.outcome == "success" and result.findings == ()
    assert result.blocks[0].text == text
    assert result.blocks[0].page_index == 0
    if file_backed:
        assert source.read_bytes() == data


@pytest.mark.parametrize("data", [b"hello", b"123456", b"\xff"])
def test_file_read_is_complete_and_handle_is_closed(tmp_path, monkeypatch, data):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"x")
    sizes = []

    class RecordingStream(BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    stream = RecordingStream(data)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=S_IFREG,
            st_size=20 * 1024 * 1024,
        ),
    )
    result = PlainTextReader().read(path)
    assert sizes == [-1]
    assert stream.closed
    if data != b"\xff":
        assert result.outcome == "success" and result.blocks[0].text == data.decode()
    else:
        assert result.outcome == "failed" and result.blocks == ()


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_unreadable_source_returns_a_safe_finding(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-filename.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_bytes(b"text")

        def denied(*args, **kwargs):
            raise PermissionError("private-error-with-sensitive-detail")

        monkeypatch.setattr(Path, "open", denied)
    result = PlainTextReader().read(Artifact(path=path, password="private-password"))
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == "source_unreadable"
    assert "private-" not in result.model_dump_json()


def test_non_regular_source_is_not_opened(monkeypatch, tmp_path):
    import stat

    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("A pipe must not be opened")

    monkeypatch.setattr(Path, "open", forbidden)
    result = PlainTextReader().read(tmp_path / "pipe.txt")
    assert result.outcome == "failed"
    assert result.findings[0].code == "source_unreadable"


@pytest.mark.parametrize("selection", [None, ("first",), ["second", "first"]])
def test_region_requests_fail_explicitly_without_opening_source(
    tmp_path, monkeypatch, layout, selection
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported selection must not access the source")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = PlainTextReader().read(
        tmp_path / "not-opened.txt", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and result.blocks == ()
    expected = ("first", "second") if selection is None else tuple(selection)
    assert tuple(finding.region_id for finding in result.findings) == expected
    assert all(finding.code == "selection_unsupported" for finding in result.findings)


@pytest.mark.parametrize("selection", [(), []])
def test_a_layout_is_refused_even_when_nothing_is_selected(tmp_path, layout, selection):
    result = PlainTextReader().read(
        tmp_path / "missing.txt", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


def test_a_map_without_regions_is_refused_too(tmp_path, layout):
    empty = LayoutAnalysisResult(outcome="success", surfaces=layout.surfaces)
    result = PlainTextReader().read(tmp_path / "missing.txt", layout=empty)
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_map_is_invalid_configuration(outcome):
    layout = LayoutAnalysisResult(
        outcome=outcome, findings=(LayoutFinding(code="example", message="No map."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        PlainTextReader().read(b"text", layout=layout)


@pytest.mark.parametrize("ids", [(), ("first",)])
def test_region_ids_require_layout(ids):
    with pytest.raises(ValueError, match="require a layout"):
        PlainTextReader().read(b"text", region_ids=ids)


@pytest.mark.parametrize(
    "ids,error",
    [
        ("first", TypeError),
        (b"first", TypeError),
        (42, TypeError),
        ([None], TypeError),
        (("first", "first"), ValueError),
        (("missing",), ValueError),
        (("first", "missing"), ValueError),
    ],
)
def test_invalid_region_selection_is_rejected(layout, ids, error):
    with pytest.raises(error):
        PlainTextReader().read(b"text", layout=layout, region_ids=ids)


@pytest.mark.parametrize("source", [None, 12, bytearray(b"text"), BytesIO(b"text")])
def test_unsupported_input_types_raise_clear_errors(source):
    with pytest.raises(TypeError, match="source must be"):
        PlainTextReader().read(source)


def test_invalid_layout_and_removed_policy_argument_raise_clear_errors():
    with pytest.raises(TypeError, match="layout must be"):
        PlainTextReader().read(b"text", layout={})
    with pytest.raises(TypeError):
        PlainTextReader(policy={})


@pytest.mark.parametrize(
    "encoding,error",
    [
        ("unknown", ValueError),
        ("base64_codec", ValueError),
        ("", ValueError),
        ("UTF-8", ValueError),
        (" utf-8 ", ValueError),
        (None, TypeError),
        (12, TypeError),
        (True, TypeError),
        (b"utf-8", TypeError),
    ],
)
def test_invalid_encoding_is_a_configuration_error(encoding, error):
    with pytest.raises(error, match="encoding must be"):
        PlainTextReader(encoding=encoding)


def test_plain_reader_has_no_policy_api():
    import raggae.documents.text_reader as public
    import raggae.documents.text_reader.plain as plain

    assert not hasattr(PlainTextReader(), "policy")
    assert not hasattr(public, "PlainTextPolicy")
    assert not hasattr(plain, "PlainTextPolicy")
