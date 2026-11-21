"""PyMuPDF4LLM structured Markdown, one block per page, OCR always off."""

import builtins
import socket
import subprocess
import sys
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    PyMuPdf4LlmTextReader,
    PyMuPdfTextReader,
    TextReader,
    TextReadResult,
)

REPORT = (
    b"BT /F1 14 Tf 30 380 Td (Purchase report) Tj ET\n"
    b"BT /F1 12 Tf 30 330 Td (Shipping labels) Tj 0 -14 Td (Cost: 90 USD) Tj ET"
)


@pytest.fixture(autouse=True)
def backend():
    return pytest.importorskip("pymupdf4llm", reason="Install raggae[text-pymupdf4llm]")


def text_page(text, x=30, y=330):
    return f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET".encode()


def pages(result):
    return [(block.page_index, block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


def read(data, **kwargs):
    return PyMuPdf4LlmTextReader().read(data, **kwargs)


def test_pdf_family_exports_the_same_worker():
    from raggae.documents.text_reader import pdf as pdf_readers

    assert pdf_readers.PyMuPdf4LlmTextReader is PyMuPdf4LlmTextReader


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
    reader = PyMuPdf4LlmTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pymupdf4llm"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert pages(result) == [
        (0, "# Purchase report\n\nShipping labels Cost: 90 USD"),
        (1, "Follow-up note"),
    ]
    for block in result.blocks:
        assert block.region_id is None
        assert block.content_format == "markdown" and block.method == "native"
        assert all(line == line.rstrip() for line in block.text.splitlines())
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_each_page_keeps_its_page_index_including_after_empty_pages(make_reader_pdf):
    data = make_reader_pdf((text_page("First"), b"", text_page("Third")))
    result = read(data)
    assert result.outcome == "success"
    assert pages(result) == [(0, "First"), (2, "Third")]
    assert [(f.code, f.page_index) for f in result.findings] == [("empty", 1)]


def test_ocr_is_always_off_and_images_are_never_written(monkeypatch, make_reader_pdf):
    import pymupdf4llm

    calls = []
    real = pymupdf4llm.to_markdown

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pymupdf4llm, "to_markdown", spy)
    read(make_reader_pdf((text_page("One"), text_page("Two"))))
    assert [call["pages"] for call in calls] == [[0], [1]]
    for call in calls:
        assert call["use_ocr"] is False and call["force_ocr"] is False
        assert call["write_images"] is False and call["embed_images"] is False
        assert call["page_chunks"] is True and call["show_progress"] is False


def test_image_only_page_is_empty_not_recognized(make_reader_pdf):
    data = make_reader_pdf((b"q 2 0 0 2 30 30 cm /Im1 Do Q",), image=True)
    result = read(data)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]
    assert "OCR is off" in result.findings[0].message


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_are_read(make_reader_pdf, rotation):
    data = make_reader_pdf((text_page("Turned page"),), rotation=rotation)
    assert [text for _, text in pages(read(data))] == [
        text for _, text in pages(read(make_reader_pdf((text_page("Turned page"),))))
    ]
    assert pages(read(data))[0][1].endswith("Turned page")


def test_rotation_reset_never_touches_the_source(make_reader_pdf, tmp_path):
    import pikepdf

    data = make_reader_pdf((text_page("Turned"),), rotation=90)
    path = tmp_path / "turned.pdf"
    path.write_bytes(data)
    read(path)
    assert path.read_bytes() == data
    with pikepdf.Pdf.open(path) as document:
        assert int(document.pages[0].obj.Rotate) == 90


def test_text_outside_the_visible_area_is_left_out(make_reader_pdf):
    data = make_reader_pdf(
        (text_page("Inside") + b"\n" + text_page("Outside crop", y=30),),
        crop=[0, 200, 300, 400],
    )
    assert pages(read(data)) == [(0, "Inside")]


def test_never_opens_network_connections(monkeypatch, make_reader_pdf):
    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert read(make_reader_pdf((REPORT,))).outcome == "success"


def test_importing_the_package_does_not_import_pymupdf4llm():
    code = (
        "import sys; import raggae.documents.text_reader; "
        "assert 'pymupdf4llm' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_pymupdf_text_reader_output_is_unaffected_by_pymupdf4llm(make_reader_pdf):
    # Importing pymupdf4llm activates PyMuPDF's layout engine process-wide.
    script = """
import sys
from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.text_reader import PyMuPdfTextReader
data = open(sys.argv[1], "rb").read()
layout = NativePdfLayoutAnalyzer().analyze(data)
def snapshot():
    return ([b.text for b in PyMuPdfTextReader().read(data).blocks],
            [b.text for b in PyMuPdfTextReader().read(data, layout=layout).blocks])
before = snapshot()
import pymupdf4llm
assert before == snapshot(), (before, snapshot())
"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(make_reader_pdf((REPORT,), rotation=90, unit=2))
        handle.flush()
        subprocess.run([sys.executable, "-c", script, handle.name], check=True)


def test_unconvertible_page_keeps_other_pages(monkeypatch, make_reader_pdf):
    import pymupdf4llm

    real = pymupdf4llm.to_markdown

    def flaky(document, *, pages, **kwargs):
        if pages == [1]:
            raise RuntimeError("private backend detail")
        return real(document, pages=pages, **kwargs)

    monkeypatch.setattr(pymupdf4llm, "to_markdown", flaky)
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


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"%PDF-1.7\nprivate garbage", "failed", "decode_failed"),
        (b"private plain text", "not_applicable", "not_applicable"),
    ],
)
def test_empty_broken_and_non_pdf_sources(data, outcome, code):
    result = read(data)
    assert result.outcome == outcome and not result.blocks
    assert codes(result) == [code]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = PyMuPdf4LlmTextReader()
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


def test_missing_backend_names_the_opt_in_extra_and_licence(
    monkeypatch, make_reader_pdf
):
    data = make_reader_pdf()
    real_import = builtins.__import__

    def without_backend(name, *args, **kwargs):
        if name == "pymupdf4llm" or name.startswith("pymupdf4llm."):
            raise ImportError("no pymupdf4llm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_backend)
    with pytest.raises(ImportError, match=r"raggae\[text-pymupdf4llm\].*AGPL"):
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
    reader = PyMuPdf4LlmTextReader()
    result = reader.read(tmp_path / "missing.pdf", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.code for f in result.findings} == {"selection_unsupported"}
    empty = reader.read(tmp_path / "missing.pdf", layout=layout, region_ids=[])
    assert empty.outcome == "failed" and not empty.blocks
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]


def test_streams_are_not_silently_added_to_source_contract(make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        PyMuPdf4LlmTextReader().read(BytesIO(make_reader_pdf()))


def test_pymupdf_reader_import_is_independent():
    assert PyMuPdfTextReader().name == "text_reader_pymupdf"
