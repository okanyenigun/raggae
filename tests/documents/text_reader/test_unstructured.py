"""Unstructured wrappers: one block per element, offline, telemetry opted out."""

import builtins
import json
import os
import subprocess
import sys
import textwrap
from io import BytesIO

import pytest

from raggae.documents.layout_analysis.region_discovery.html import HtmlLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    TextReader,
    TextReadResult,
    UnstructuredCsvTextReader,
    UnstructuredDocxTextReader,
    UnstructuredExcelTextReader,
    UnstructuredHtmlTextReader,
    UnstructuredJsonLinesTextReader,
    UnstructuredJsonTextReader,
    UnstructuredMarkdownTextReader,
    UnstructuredPptxTextReader,
    UnstructuredTextReader,
)

pytest.importorskip("unstructured", reason="Install raggae[text-unstructured]")
if __import__("importlib").util.find_spec("en_core_web_sm") is None:
    pytest.skip("spaCy model en_core_web_sm not installed", allow_module_level=True)

TEXT_READERS = [
    UnstructuredTextReader,
    UnstructuredMarkdownTextReader,
    UnstructuredJsonTextReader,
    UnstructuredJsonLinesTextReader,
    UnstructuredCsvTextReader,
    UnstructuredHtmlTextReader,
]
BINARY_READERS = [
    UnstructuredDocxTextReader,
    UnstructuredPptxTextReader,
    UnstructuredExcelTextReader,
]
ALL = TEXT_READERS + BINARY_READERS


def blocks(result):
    return [(b.page_index, b.text) for b in result.blocks]


def codes(result):
    return [f.code for f in result.findings]


def docx_bytes():
    from docx import Document

    document = Document()
    document.add_heading("Warranty", 1)
    document.add_paragraph("Covers two years for parts.")
    table = document.add_table(rows=2, cols=2)
    for (row, col), text in zip(
        [(0, 0), (0, 1), (1, 0), (1, 1)], ["Part", "Years", "Motor", "2"]
    ):
        table.cell(row, col).text = text
    document.sections[0].header.paragraphs[0].text = "Confidential"
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def pptx_bytes():
    from pptx import Presentation

    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[5])
    first.shapes.title.text = "Pricing"
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Coverage"
    first.notes_slide.notes_text_frame.text = "Private speaker note"
    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def xlsx_bytes():
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["Part", "Price"])
    workbook.active.append(["Motor", 120.5])
    workbook.create_sheet("Notes")["A1"] = "VAT excluded"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


SAMPLES = {
    UnstructuredTextReader: b"Warranty\n\nCovers two years for parts.\n\n- Motor\n- Gears",
    UnstructuredMarkdownTextReader: b"# Warranty\n\nCovers **two** years.\n\n| Part | Years |\n|---|---|\n| Motor | 2 |\n",
    UnstructuredJsonTextReader: b'{"customer": {"name": "Ada"}}',
    UnstructuredJsonLinesTextReader: b'{"id": 1}\n{"id": 2}\n',
    UnstructuredCsvTextReader: b"Part,Years\nMotor,2\n00123,3\n",
    UnstructuredHtmlTextReader: b"<h1>Warranty</h1><p>Covers two years.</p><script>track()</script>",
}


def sample(reader_class):
    if reader_class is UnstructuredDocxTextReader:
        return docx_bytes()
    if reader_class is UnstructuredPptxTextReader:
        return pptx_bytes()
    if reader_class is UnstructuredExcelTextReader:
        return xlsx_bytes()
    return SAMPLES[reader_class]


# Family contract -------------------------------------------------------------


def test_family_exports():
    from importlib import import_module

    folders = {
        "UnstructuredTextReader": "plain",
        "UnstructuredMarkdownTextReader": "markdown",
        "UnstructuredJsonTextReader": "json",
        "UnstructuredJsonLinesTextReader": "json.jsonl",
        "UnstructuredCsvTextReader": "csv",
        "UnstructuredHtmlTextReader": "html",
        "UnstructuredDocxTextReader": "docx",
        "UnstructuredPptxTextReader": "pptx",
        "UnstructuredExcelTextReader": "xlsx",
    }
    for reader_class in ALL:
        folder = import_module(
            f"raggae.documents.text_reader.{folders[reader_class.__name__]}"
        )
        assert getattr(folder, reader_class.__name__) is reader_class


@pytest.mark.parametrize("reader_class", ALL)
def test_protocol_names_schema_and_unchanged_file(tmp_path, reader_class):
    reader = reader_class()
    assert isinstance(reader, TextReader)
    assert reader.name.startswith("text_reader_unstructured_")
    data = sample(reader_class)
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    for source in (path, str(path), data, Artifact(path=path), Artifact(data=data)):
        result = reader.read(source)
        assert result.outcome == "success" and result.blocks
        assert TextReadResult.model_validate_json(result.model_dump_json()) == result
        for block in result.blocks:
            assert block.region_id is None and block.method == "native"
    assert path.read_bytes() == data


def test_plain_text_elements():
    result = UnstructuredTextReader().read(SAMPLES[UnstructuredTextReader])
    assert blocks(result) == [
        (0, "Warranty"),
        (0, "Covers two years for parts."),
        (0, "Motor"),
        (0, "Gears"),
    ]


def test_markdown_table_is_written_as_rows():
    result = UnstructuredMarkdownTextReader().read(
        SAMPLES[UnstructuredMarkdownTextReader]
    )
    assert blocks(result) == [
        (0, "Warranty"),
        (0, "Covers two years."),
        (0, "Part | Years\nMotor | 2"),
    ]


def test_csv_is_one_table_and_keeps_leading_zeros():
    result = UnstructuredCsvTextReader().read(SAMPLES[UnstructuredCsvTextReader])
    assert blocks(result) == [(0, "Part | Years\nMotor | 2\n00123 | 3")]


def test_html_skips_scripts():
    result = UnstructuredHtmlTextReader().read(SAMPLES[UnstructuredHtmlTextReader])
    assert blocks(result) == [(0, "Warranty"), (0, "Covers two years.")]


def test_json_documents_and_unstructured_element_files():
    plain = UnstructuredJsonTextReader().read(b'{"customer": {"name": "Ada"}}')
    assert json.loads(plain.blocks[0].text) == {"customer": {"name": "Ada"}}
    serialized = json.dumps(
        [{"type": "Title", "text": "Hello", "element_id": "a", "metadata": {}}]
    ).encode()
    # Unstructured turns its own serialized elements back into elements.
    assert blocks(UnstructuredJsonTextReader().read(serialized)) == [(0, "Hello")]


def test_word_elements_include_headers_and_table_rows():
    assert blocks(UnstructuredDocxTextReader().read(docx_bytes())) == [
        (0, "Confidential"),
        (0, "Warranty"),
        (0, "Covers two years for parts."),
        (0, "Part | Years\nMotor | 2"),
    ]


def test_slides_give_page_index_and_notes_are_not_read():
    result = UnstructuredPptxTextReader().read(pptx_bytes())
    assert blocks(result) == [(0, "Pricing"), (1, "Coverage")]
    assert "speaker" not in result.model_dump_json()


def test_sheets_give_page_index():
    assert blocks(UnstructuredExcelTextReader().read(xlsx_bytes())) == [
        (0, "Part | Price\nMotor | 120.5"),
        (1, "VAT excluded"),
    ]


@pytest.mark.parametrize(
    "reader_class,data",
    [
        (UnstructuredTextReader, b"   \n\n"),
        (UnstructuredHtmlTextReader, b"<html><body></body></html>"),
    ],
)
def test_no_text_succeeds_with_an_explanation(reader_class, data):
    result = reader_class().read(data)
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == ["empty"]


@pytest.mark.parametrize("reader_class", BINARY_READERS)
@pytest.mark.parametrize("data", [b"", b"private not an office file"])
def test_unreadable_binary_sources_fail_safely(reader_class, data):
    result = reader_class().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result)[0] in (
        "decode_failed",
        "parse_failed",
    )
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("reader_class", TEXT_READERS)
def test_text_readers_decode_strictly_with_the_configured_encoding(reader_class):
    result = reader_class().read(b"private\xff")
    assert codes(result) == ["decode_failed"]
    assert "private" not in result.model_dump_json()
    # Each reader gets content valid for its format: JSON readers need JSON.
    content = {
        UnstructuredJsonTextReader: '"Café"',
        UnstructuredJsonLinesTextReader: '{"name": "Café"}\n',
    }.get(reader_class, "Café\n")
    latin = reader_class(encoding="iso-8859-1").read(content.encode("iso-8859-1"))
    assert latin.outcome == "success"


@pytest.mark.parametrize("reader_class", ALL)
def test_configuration_errors(reader_class):
    with pytest.raises(ValueError):
        reader_class(encoding="unknown-codec")


# JSON Lines ------------------------------------------------------------------


def test_json_lines_keep_source_lines_and_recover_from_bad_lines():
    result = UnstructuredJsonLinesTextReader().read(
        b'{"id": 1}\nnot json\n\n{"id": 4}\r\n'
    )
    assert result.outcome == "partial"
    assert [json.loads(b.text)["line"] for b in result.blocks] == [1, 4]
    assert all(b.content_format == "json" for b in result.blocks)
    assert json.loads(json.loads(result.blocks[0].text)["value"]) == {"id": 1}
    assert [f.message for f in result.findings] == [
        "Line 2 could not be read as one JSON value.",
        "Line 3 could not be read as one JSON value.",
    ]


def test_json_lines_all_bad_fails_and_empty_succeeds():
    assert UnstructuredJsonLinesTextReader().read(b"nope\n").outcome == "failed"
    empty = UnstructuredJsonLinesTextReader().read(b"")
    assert empty.outcome == "success"
    assert codes(empty) == ["empty"]


# Safeguards ------------------------------------------------------------------

OFFLINE_RUN = textwrap.dedent(
    """
    import os, socket, subprocess, urllib.request, sys
    def blocked(*a, **k):
        raise AssertionError("network or subprocess used: %r" % (a[:1],))
    socket.create_connection = blocked
    socket.socket.connect = blocked
    urllib.request.urlopen = blocked
    subprocess.run = blocked
    subprocess.Popen = blocked
    import raggae.documents.text_reader as t
    assert "unstructured" not in sys.modules
    assert not os.environ.get("SCARF_NO_ANALYTICS") and not os.environ.get("DO_NOT_TRACK")
    result = t.UnstructuredTextReader().read(b"Warranty\\n\\nCovers two years.")
    assert result.outcome == "success", result
    assert os.environ["SCARF_NO_ANALYTICS"] == "true"
    samples = {
        "UnstructuredMarkdownTextReader": b"# Hello\\n\\nworld",
        "UnstructuredHtmlTextReader": b"<p>Hello, world</p>",
        "UnstructuredCsvTextReader": b"a,b\\n1,2\\n",
        "UnstructuredJsonTextReader": b'{"greeting": "Hello"}',
        "UnstructuredJsonLinesTextReader": b'{"id": 1}\\n',
    }
    for name, data in samples.items():
        assert getattr(t, name)().read(data).outcome == "success", name
    print("ok")
    """
)


def clean_env(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SCARF_NO_ANALYTICS", "DO_NOT_TRACK")
    }
    env.update(extra)
    return env


def test_reads_never_touch_the_network_or_run_subprocesses_and_opt_out():
    # A fresh process, so no earlier import of unstructured can hide a ping.
    completed = subprocess.run(
        [sys.executable, "-c", OFFLINE_RUN],
        env=clean_env(),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "ok"


def test_an_existing_opt_out_is_respected():
    code = (
        "import os, raggae.documents.text_reader as t; "
        "t.UnstructuredTextReader().read(b'x'); "
        "assert os.environ.get('SCARF_NO_ANALYTICS') is None; print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=clean_env(DO_NOT_TRACK="1"),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_importing_the_package_neither_imports_unstructured_nor_sets_env():
    code = (
        "import os, sys, raggae.documents.text_reader; "
        "assert 'unstructured' not in sys.modules; "
        "assert 'SCARF_NO_ANALYTICS' not in os.environ; print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], env=clean_env(), capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_missing_spacy_model_is_an_error_never_an_install(monkeypatch):
    import importlib.util

    import unstructured.nlp.tokenize as tokenize

    def never(*args, **kwargs):
        raise AssertionError("Unstructured must not install the spaCy model")

    monkeypatch.setattr(tokenize, "_install_spacy_model", never)
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "en_core_web_sm" else real(name, *a, **k),
    )
    with pytest.raises(RuntimeError, match="python -m spacy download en_core_web_sm"):
        UnstructuredTextReader().read(b"Hello")


def test_missing_backend_has_an_install_hint(monkeypatch):
    real_import = builtins.__import__

    def without_unstructured(name, *args, **kwargs):
        if name == "unstructured" or name.startswith("unstructured."):
            raise ImportError("no unstructured")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_unstructured)
    with pytest.raises(ImportError, match=r"raggae\[text-unstructured\]"):
        UnstructuredTextReader().read(b"Hello")


# Regions and sources ---------------------------------------------------------


@pytest.mark.parametrize("reader_class", ALL)
def test_layout_regions_are_rejected_before_source_io(
    tmp_path, monkeypatch, reader_class
):
    layout = HtmlLayoutAnalyzer().analyze(b"<p>Hello</p>")
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    reader = reader_class()
    result = reader.read(tmp_path / "missing", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert set(codes(result)) == {"selection_unsupported"}
    empty = reader.read(tmp_path / "missing", layout=layout, region_ids=[])
    assert empty.outcome == "failed" and not empty.blocks
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]


@pytest.mark.parametrize("reader_class", ALL)
def test_missing_files_and_streams(tmp_path, reader_class):
    result = reader_class().read(tmp_path / "private-name")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    with pytest.raises(TypeError, match="source must"):
        reader_class().read(BytesIO(b"x"))
