"""Docling PDF elements: one block per element with its page, offline, no OCR."""

import builtins
import os
import socket
import sys
from io import BytesIO
from pathlib import Path
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    DoclingPdfTextReader,
    TextReader,
    TextReadResult,
)

pytest.importorskip("docling", reason="Install raggae[text-docling]")

from docling.datamodel.base_models import ConversionStatus  # noqa: E402
from docling_core.types.doc import (  # noqa: E402
    BoundingBox,
    ContentLayer,
    DocItemLabel,
    DoclingDocument,
    GroupLabel,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)

MODELS = Path(
    os.environ.get(
        "RAGGAE_DOCLING_MODELS", Path.home() / ".cache" / "raggae" / "docling"
    )
)


def prov(page):
    return ProvenanceItem(
        page_no=page, bbox=BoundingBox(l=10, t=10, r=100, b=30), charspan=(0, 1)
    )


def document(pages=2):
    doc = DoclingDocument(name="source")
    for number in range(1, pages + 1):
        doc.add_page(page_no=number, size=Size(width=300, height=400))
    return doc


def table(doc, rows, page):
    cells = [
        TableCell(
            text=text,
            start_row_offset_idx=r,
            end_row_offset_idx=r + 1,
            start_col_offset_idx=c,
            end_col_offset_idx=c + 1,
            column_header=r == 0,
        )
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    ]
    doc.add_table(
        data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells),
        prov=prov(page),
    )


class FakeConverter:
    """Stands in for Docling's converter; returns a prepared document."""

    def __init__(self, doc, status=ConversionStatus.SUCCESS):
        self.doc, self.status, self.calls = doc, status, []

    def convert(self, source, *, raises_on_error):
        self.calls.append((source.name, source.stream.getvalue(), raises_on_error))
        return SimpleNamespace(status=self.status, document=self.doc)


@pytest.fixture
def models(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    return folder


def reader_with(doc, models, status=ConversionStatus.SUCCESS):
    reader = DoclingPdfTextReader(models_path=models)
    reader._converter = FakeConverter(doc, status)
    return reader


def blocks(result):
    return [(b.page_index, b.content_format, b.text) for b in result.blocks]


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


# Contract (stand-in converter) -------------------------------------------


def test_pdf_family_exports_the_same_worker():
    from raggae.documents.text_reader import pdf as pdf_readers

    assert pdf_readers.DoclingPdfTextReader is DoclingPdfTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, models, make_reader_pdf, source_kind
):
    doc = document(1)
    doc.add_heading(text="Purchase report", prov=prov(1))
    data = make_reader_pdf()
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path),
        "artifact_bytes": Artifact(data=data),
    }[source_kind]
    reader = reader_with(doc, models)
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_docling_pdf"
    result = reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert blocks(result) == [(0, "plain_text", "Purchase report")]
    assert result.blocks[0].region_id is None and result.blocks[0].method == "native"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert reader._converter.calls[0][1:] == (data, False)
    assert path.read_bytes() == data and sorted(tmp_path.iterdir()) == sorted(
        [path, models]
    )


def test_one_block_per_element_with_its_page_and_markdown_tables(
    models, make_reader_pdf
):
    doc = document(2)
    doc.add_heading(text="Purchase report", prov=prov(1))
    doc.add_text(
        label=DocItemLabel.TEXT, text="  Shipping labels were ordered.  ", prov=prov(1)
    )
    items = doc.add_group(label=GroupLabel.LIST)
    doc.add_list_item(text="Tape", prov=prov(1), parent=items)
    table(doc, [["Part", "Years"], ["Motor", "2"]], page=2)
    doc.add_text(label=DocItemLabel.CAPTION, text="Coverage", prov=prov(2))
    result = reader_with(doc, models).read(make_reader_pdf((b"", b"")))
    assert result.outcome == "success" and not result.findings
    assert blocks(result) == [
        (0, "plain_text", "Purchase report"),
        (0, "plain_text", "Shipping labels were ordered."),
        (0, "plain_text", "Tape"),
        (
            1,
            "markdown",
            "| Part   |   Years |\n|--------|---------|\n| Motor  |       2 |",
        ),
        (1, "plain_text", "Coverage"),
    ]


def test_every_content_layer_is_read(models, make_reader_pdf):
    doc = document(1)
    for text, layer in (
        ("ACME Ltd - Confidential", ContentLayer.FURNITURE),
        ("Purchase report", ContentLayer.BODY),
        ("DRAFT", ContentLayer.BACKGROUND),
        ("Scanned text from an OCR layer", ContentLayer.INVISIBLE),
        ("Speaker note", ContentLayer.NOTES),
    ):
        doc.add_text(
            label=DocItemLabel.TEXT, text=text, prov=prov(1), content_layer=layer
        )
    result = reader_with(doc, models).read(make_reader_pdf())
    assert [b.text for b in result.blocks] == [
        "ACME Ltd - Confidential",
        "Purchase report",
        "DRAFT",
        "Scanned text from an OCR layer",
        "Speaker note",
    ]


def test_empty_pages_and_pictures_are_reported(models, make_reader_pdf):
    doc = document(3)
    doc.add_heading(text="First", prov=prov(1))
    doc.add_picture(prov=prov(3))
    result = reader_with(doc, models).read(make_reader_pdf((b"", b"", b"")))
    assert result.outcome == "success"
    assert blocks(result) == [(0, "plain_text", "First")]
    assert codes(result) == [
        ("empty", 1),
        ("pictures_unread", 2),
        ("empty", 2),
    ]


def test_whitespace_only_elements_give_no_block(models, make_reader_pdf):
    doc = document(1)
    doc.add_text(label=DocItemLabel.TEXT, text="   ", prov=prov(1))
    result = reader_with(doc, models).read(make_reader_pdf())
    assert not result.blocks
    assert codes(result) == [("empty", 0)]


def test_partial_conversion_keeps_content_and_says_so(models, make_reader_pdf):
    doc = document(1)
    doc.add_text(label=DocItemLabel.TEXT, text="Kept", prov=prov(1))
    result = reader_with(doc, models, ConversionStatus.PARTIAL_SUCCESS).read(
        make_reader_pdf()
    )
    assert result.outcome == "partial"
    assert [b.text for b in result.blocks] == ["Kept"]
    assert codes(result) == [("conversion_partial", None)]


@pytest.mark.parametrize("status", [ConversionStatus.FAILURE, ConversionStatus.SKIPPED])
def test_failed_conversion_is_decode_failed(models, make_reader_pdf, status):
    result = reader_with(document(1), models, status).read(make_reader_pdf())
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == [("decode_failed", None)]


def test_passwords_are_applied_before_docling_sees_the_file(models, make_reader_pdf):
    import pikepdf

    data = make_reader_pdf(password="private-pass")
    doc = document(1)
    doc.add_text(label=DocItemLabel.TEXT, text="Secret", prov=prov(1))
    reader = reader_with(doc, models)
    opened = reader.read(Artifact(data=data, password="private-pass"))
    assert [b.text for b in opened.blocks] == ["Secret"]
    handed = reader._converter.calls[-1][1]
    with pikepdf.Pdf.open(BytesIO(handed)) as decrypted:
        assert not decrypted.is_encrypted
    assert "private-pass" not in opened.model_dump_json()
    for given, code in (
        (None, "password_required"),
        ("wrong-private", "invalid_password"),
    ):
        result = reader.read(Artifact(data=data, password=given))
        assert result.outcome == "failed"
        assert codes(result) == [(code, None)]
        assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"%PDF-1.7\nprivate garbage", "failed", "decode_failed"),
        (b"private plain text", "not_applicable", "not_applicable"),
    ],
)
def test_empty_broken_and_non_pdf_sources_never_reach_docling(
    models, data, outcome, code
):
    reader = reader_with(document(1), models)
    result = reader.read(data)
    assert result.outcome == outcome and not result.blocks
    assert codes(result) == [(code, None)]
    assert reader._converter.calls == []
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, models, monkeypatch):
    reader = reader_with(document(1), models)
    result = reader.read(tmp_path / "private-name.pdf")
    assert codes(result) == [("source_unreadable", None)]
    assert "private-name" not in result.model_dump_json()
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


@pytest.mark.parametrize(
    "value,error", [(None, TypeError), (3, TypeError), ("/no/such/folder", ValueError)]
)
def test_models_path_is_validated_at_construction(tmp_path, value, error):
    with pytest.raises(error):
        DoclingPdfTextReader(models_path=value)
    file = tmp_path / "not-a-folder"
    file.write_bytes(b"x")
    with pytest.raises(ValueError):
        DoclingPdfTextReader(models_path=file)


def test_models_path_is_required():
    with pytest.raises(TypeError):
        DoclingPdfTextReader()  # type: ignore[call-arg]


def test_folder_without_models_is_a_configuration_error(models, make_reader_pdf):
    reader = DoclingPdfTextReader(models_path=models)
    with pytest.raises(ValueError, match="models_path does not contain usable Docling"):
        reader.read(make_reader_pdf())


def test_missing_backend_has_an_install_hint(monkeypatch, models, make_reader_pdf):
    real_import = builtins.__import__

    def without_docling(name, *args, **kwargs):
        if name == "docling" or name.startswith("docling."):
            raise ImportError("no docling")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_docling)
    with pytest.raises(ImportError, match=r"raggae\[text-docling\]"):
        DoclingPdfTextReader(models_path=models).read(make_reader_pdf())


def test_importing_the_package_does_not_import_docling():
    import subprocess

    code = (
        "import sys; import raggae.documents.text_reader; "
        "assert not any(m.startswith('docling') for m in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_layout_regions_are_rejected_before_source_io(
    tmp_path, monkeypatch, models, make_reader_pdf
):
    layout = NativePdfLayoutAnalyzer().analyze(make_reader_pdf())
    reader = reader_with(document(1), models)
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = reader.read(tmp_path / "missing.pdf", layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.code for f in result.findings} == {"selection_unsupported"}
    empty = reader.read(tmp_path / "missing.pdf", layout=layout, region_ids=[])
    assert empty.outcome == "failed" and not empty.blocks
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]


def test_streams_are_not_silently_added_to_source_contract(models, make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        reader_with(document(1), models).read(BytesIO(make_reader_pdf()))


# Real pipeline (needs downloaded models) ---------------------------------

needs_models = pytest.mark.skipif(
    not (MODELS / "docling-project--docling-models").is_dir(),
    reason="Docling models not downloaded (set RAGGAE_DOCLING_MODELS)",
)


@pytest.fixture(scope="module")
def real_reader():
    return DoclingPdfTextReader(models_path=MODELS)


@needs_models
def test_real_pipeline_offline_pages_and_reuse(
    real_reader, make_reader_pdf, monkeypatch
):
    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    data = make_reader_pdf(
        (
            b"BT /F1 16 Tf 30 380 Td (Purchase report) Tj ET\n"
            b"BT /F1 11 Tf 30 340 Td (Shipping labels were ordered on Monday.) Tj ET",
            b"",
            b"BT /F1 11 Tf 30 330 Td (Follow-up note) Tj ET",
        )
    )
    result = real_reader.read(data)
    assert result.outcome == "success"
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (0, "Purchase report"),
        (0, "Shipping labels were ordered on Monday."),
        (2, "Follow-up note"),
    ]
    assert codes(result) == [("empty", 1)]
    converter = real_reader._converter
    real_reader.read(data)
    assert real_reader._converter is converter  # Models load once per reader.


@needs_models
@pytest.mark.parametrize("rotation", [90, 180])
def test_real_pipeline_rotated_pages(real_reader, make_reader_pdf, rotation):
    data = make_reader_pdf(
        (b"BT /F1 11 Tf 30 330 Td (Rotated page text here) Tj ET",), rotation=rotation
    )
    assert [b.text for b in real_reader.read(data).blocks] == ["Rotated page text here"]


@needs_models
def test_real_pipeline_crop_password_and_picture(real_reader, make_reader_pdf):
    cropped = make_reader_pdf(
        (
            b"BT /F1 11 Tf 30 330 Td (Inside text) Tj ET\n"
            b"BT /F1 11 Tf 30 30 Td (Outside crop text) Tj ET",
        ),
        crop=[0, 200, 300, 400],
    )
    assert [b.text for b in real_reader.read(cropped).blocks] == ["Inside text"]
    locked = make_reader_pdf(
        (b"BT /F1 11 Tf 30 330 Td (Secret text) Tj ET",), password="pass"
    )
    assert [
        b.text for b in real_reader.read(Artifact(data=locked, password="pass")).blocks
    ] == ["Secret text"]
    picture = real_reader.read(
        make_reader_pdf((b"q 200 0 0 200 50 100 cm /Im1 Do Q",), image=True)
    )
    assert picture.outcome == "success" and not picture.blocks
    assert ("pictures_unread", 0) in codes(picture)
