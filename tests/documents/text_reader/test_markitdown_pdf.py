"""MarkItDown PDF conversion, one Markdown block per page, offline."""

import builtins
import socket
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    MarkItDownPdfTextReader,
    TextReader,
    TextReadResult,
)

REPORT = (
    b"BT /F1 14 Tf 30 380 Td (Purchase report) Tj ET\n"
    b"BT /F1 12 Tf 30 330 Td (Shipping labels) Tj 0 -14 Td (Cost: 90 USD) Tj ET\n"
    b"BT /F1 12 Tf 180 330 Td (Packing tape) Tj 0 -14 Td (Cost: 18 USD) Tj ET"
)


@pytest.fixture(autouse=True)
def backend():
    return pytest.importorskip("markitdown", reason="Install raggae[text-markitdown]")


def text_page(text, x=30, y=330):
    return f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET".encode()


def pages(result):
    return [(block.page_index, block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def read(data, **kwargs):
    return MarkItDownPdfTextReader().read(data, **kwargs)


def test_pdf_family_exports_the_same_worker():
    from raggae.documents.text_reader import pdf as pdf_readers

    assert pdf_readers.MarkItDownPdfTextReader is MarkItDownPdfTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, make_reader_pdf, source_kind
):
    data = make_reader_pdf((REPORT, text_page("Follow-up note")))
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path),
        "artifact_bytes": Artifact(data=data),
    }[source_kind]
    reader = MarkItDownPdfTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_markitdown_pdf"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert pages(result) == [
        (
            0,
            "Purchase report\n\nShipping labels\nCost: 90 USD\n\nPacking tape\nCost: 18 USD",
        ),
        (1, "Follow-up note"),
    ]
    for block in result.blocks:
        assert block.region_id is None and "\f" not in block.text
        assert block.content_format == "markdown" and block.method == "native"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_each_page_keeps_its_page_index_including_after_empty_pages(make_reader_pdf):
    data = make_reader_pdf((text_page("First"), b"", text_page("Third")))
    result = read(data)
    assert result.outcome == "success"
    assert pages(result) == [(0, "First"), (2, "Third")]
    assert [(f.code, f.page_index) for f in result.findings] == [("empty", 1)]


def test_page_text_matches_a_whole_document_conversion(make_reader_pdf):
    from markitdown import MarkItDown, StreamInfo

    data = make_reader_pdf((REPORT, text_page("Follow-up note")))
    whole = MarkItDown(enable_plugins=False).convert_stream(
        BytesIO(data), stream_info=StreamInfo(extension=".pdf")
    )
    joined = "\n\n".join(text for _, text in pages(read(data)))
    assert " ".join(joined.split()) == " ".join(
        whole.text_content.replace("\f", "").split()
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_keep_words_together(make_reader_pdf, rotation):
    data = make_reader_pdf((text_page("Turned page"),), rotation=rotation)
    assert pages(read(data)) == [(0, "Turned page")]


def test_rotation_is_removed_only_from_the_internal_copy(make_reader_pdf, tmp_path):
    import pikepdf

    data = make_reader_pdf((text_page("Turned"),), rotation=90)
    path = tmp_path / "turned.pdf"
    path.write_bytes(data)
    read(path)
    with pikepdf.Pdf.open(path) as document:
        assert int(document.pages[0].obj.Rotate) == 90


def test_text_outside_the_visible_area_is_included_and_reported(make_reader_pdf):
    data = make_reader_pdf(
        (text_page("Inside") + b"\n" + text_page("Outside crop", y=30),),
        crop=[0, 200, 300, 400],
    )
    result = read(data)
    assert result.outcome == "success"
    assert pages(result) == [(0, "Inside\n\nOutside crop")]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("crop_not_applied", 0)
    ]


def test_only_the_pdf_converter_runs_and_plugins_stay_off(monkeypatch, make_reader_pdf):
    import markitdown

    created = []
    real = markitdown.MarkItDown

    class Spy(real):
        def __init__(self, **kwargs):
            created.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(markitdown, "MarkItDown", Spy)
    assert read(make_reader_pdf((text_page("x"),))).outcome == "success"
    assert created == [{"enable_builtins": False, "enable_plugins": False}]


def test_never_opens_network_connections(monkeypatch, make_reader_pdf):
    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert pages(read(make_reader_pdf((REPORT,))))[0][1].startswith("Purchase report")


def test_unconvertible_page_keeps_other_pages(monkeypatch, make_reader_pdf):
    from markitdown import FileConversionException, MarkItDown

    real = MarkItDown.convert_stream
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise FileConversionException("private backend detail")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(MarkItDown, "convert_stream", flaky)
    data = make_reader_pdf((text_page("One"), text_page("Two"), text_page("Three")))
    result = read(data)
    assert result.outcome == "partial"
    assert pages(result) == [(0, "One"), (2, "Three")]
    assert [(f.code, f.page_index) for f in result.findings] == [("page_unreadable", 1)]
    assert "private" not in result.model_dump_json()


# Passwords and invalid sources ------------------------------------------


def test_passwords_are_applied_and_never_revealed(make_reader_pdf):
    data = make_reader_pdf((text_page("Secret"),), password="private-pass")
    opened = read(Artifact(data=data, password="private-pass"))
    assert pages(opened) == [(0, "Secret")]
    assert "private-pass" not in opened.model_dump_json()
    for given, code in (
        (None, "password_required"),
        ("wrong-private", "invalid_password"),
    ):
        result = read(Artifact(data=data, password=given))
        assert result.outcome == "failed" and not result.blocks
        assert codes(result) == [code]
        assert "private" not in result.model_dump_json()
    assert pages(read(make_reader_pdf((text_page("Open"),), password=""))) == [
        (0, "Open")
    ]


def test_broken_pdf_is_never_converted_as_plain_text():
    result = read(b"%PDF-1.7\nprivate garbage")
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"private plain text", "not_applicable", "not_applicable"),
    ],
)
def test_empty_and_non_pdf_sources(data, outcome, code):
    result = read(data)
    assert result.outcome == outcome and not result.blocks
    assert codes(result) == [code]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = MarkItDownPdfTextReader()
    result = reader.read(tmp_path / "private-name.pdf")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


def test_missing_backend_has_an_install_hint(monkeypatch, make_reader_pdf):
    data = make_reader_pdf()
    real_import = builtins.__import__

    def without_markitdown(name, *args, **kwargs):
        if name == "markitdown" or name.startswith("markitdown."):
            raise ImportError("no markitdown")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_markitdown)
    with pytest.raises(ImportError, match=r"raggae\[text-markitdown\]"):
        read(data)


# Layout regions -------------------------------------------------------------


def test_layout_regions_are_rejected_before_source_io(
    tmp_path, monkeypatch, make_reader_pdf
):
    layout = NativePdfLayoutAnalyzer().analyze(make_reader_pdf((REPORT,)))
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    reader = MarkItDownPdfTextReader()
    result = reader.read(tmp_path / "missing.pdf", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.code for f in result.findings} == {"selection_unsupported"}
    assert [f.region_id for f in result.findings] == [r.id for r in layout.regions]
    empty = reader.read(tmp_path / "missing.pdf", layout=layout, region_ids=[])
    assert empty.outcome == "failed" and not empty.blocks
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]


def test_streams_are_not_silently_added_to_source_contract(make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        MarkItDownPdfTextReader().read(BytesIO(make_reader_pdf()))
