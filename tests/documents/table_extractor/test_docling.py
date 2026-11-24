"""Tables Docling's TableFormer model recognizes in a PDF, offline, from local models."""

import builtins
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from raggae.documents.normalization import Artifact
from raggae.documents.table_extractor import (
    DoclingTableExtractor,
    TableExtractor,
    TableExtractResult,
)
from tests.documents.image_extractor.conftest import make_layout
from tests.documents.table_extractor.conftest import RULED, make_pdf, shipments

pytest.importorskip("docling", reason="Install raggae[text-docling]")

from docling.datamodel.base_models import ConversionStatus  # noqa: E402
from docling_core.types.doc import (  # noqa: E402
    BoundingBox,
    ContentLayer,
    DoclingDocument,
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


def cell(text, row, column, rows=1, columns=1, **flags):
    return TableCell(
        text=text,
        start_row_offset_idx=row,
        end_row_offset_idx=row + rows,
        start_col_offset_idx=column,
        end_col_offset_idx=column + columns,
        row_span=rows,
        col_span=columns,
        **flags,
    )


def add_table(doc, cells, rows, columns, page=1, layer=None):
    doc.add_table(
        data=TableData(num_rows=rows, num_cols=columns, table_cells=cells),
        prov=prov(page),
        content_layer=layer,
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


def extractor_with(doc, models, status=ConversionStatus.SUCCESS):
    extractor = DoclingTableExtractor(models_path=models)
    extractor._converter = FakeConverter(doc, status)
    return extractor


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def grid(table):
    return [
        (c.row, c.column, c.row_span, c.column_span, c.text, c.header)
        for c in table.cells
    ]


def prices():
    """A price table on page 2: "Part" over two header rows, "Price" over two columns."""
    doc = document()
    add_table(
        doc,
        [
            cell("Part", 0, 0, rows=2, column_header=True),
            cell("Price", 0, 1, columns=2, column_header=True),
            cell("2025", 1, 1, column_header=True),
            cell("2026", 1, 2, column_header=True),
            cell("Motor", 2, 0, row_header=True),
            cell("110", 2, 1),
            cell("120", 2, 2),
        ],
        rows=3,
        columns=3,
        page=2,
    )
    return doc


PRICES = [
    (0, 0, 2, 1, "Part", True),
    (0, 1, 1, 2, "Price", True),
    (1, 1, 1, 1, "2025", True),
    (1, 2, 1, 1, "2026", True),
    (2, 0, 1, 1, "Motor", True),
    (2, 1, 1, 1, "110", False),
    (2, 2, 1, 1, "120", False),
]


def test_package_exports_the_worker():
    from raggae.documents.table_extractor import pdf

    assert pdf.DoclingTableExtractor is DoclingTableExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, models, source_kind):
    data = make_pdf(RULED)
    path = tmp_path / "prices.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(path=path),
    }[source_kind]
    extractor = extractor_with(prices(), models)
    assert isinstance(extractor, TableExtractor)
    assert extractor.name == "table_extractor_docling"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    (table,) = result.tables
    # The model's spans and its column- and row-header flags.
    assert grid(table) == PRICES
    assert (table.method, table.page_index, table.region_id, table.caption) == (
        "model",
        1,
        None,
        None,
    )
    assert extractor._converter.calls[0][1:] == (data, False)
    assert TableExtractResult.model_validate_json(result.model_dump_json()) == result
    # Nothing written beside the source (the models folder is the fixture's).
    assert path.read_bytes() == data and sorted(tmp_path.iterdir()) == sorted([path, models])


def test_tables_on_every_content_layer_in_reading_order(models):
    doc = document(1)
    for text, layer in (
        ("Body", ContentLayer.BODY),
        ("Header", ContentLayer.FURNITURE),
        ("Background", ContentLayer.BACKGROUND),
    ):
        add_table(doc, [cell(text, 0, 0)], rows=1, columns=1, layer=layer)
    result = extractor_with(doc, models).extract(make_pdf(RULED))
    assert [t.cells[0].text for t in result.tables] == ["Body", "Header", "Background"]


def test_spans_past_the_grid_and_repeated_or_stray_cells():
    doc = document(1)
    add_table(
        doc,
        [
            cell("wide", 0, 0, columns=5),
            cell("again", 0, 0),
            cell("stray", 4, 0),
            cell("b", 1, 1),
        ],
        rows=2,
        columns=2,
    )
    extractor = DoclingTableExtractor(models_path=Path.home())
    extractor._converter = FakeConverter(doc)
    result = extractor.extract(make_pdf(RULED))
    assert grid(result.tables[0]) == [
        (0, 0, 1, 2, "wide", False),
        (1, 1, 1, 1, "b", False),
    ]
    assert codes(result) == [("span_shortened", 0)]


def test_tables_without_text_and_pdfs_without_tables(models):
    doc = document(1)
    add_table(doc, [cell("", 0, 0), cell(" ", 0, 1)], rows=1, columns=2)
    result = extractor_with(doc, models).extract(make_pdf(RULED))
    assert not result.tables and codes(result) == [("empty_table", 0)]
    result = extractor_with(document(1), models).extract(make_pdf(RULED))
    assert result.outcome == "success" and codes(result) == [("empty", None)]


def test_partial_and_failed_conversions(models):
    partial = extractor_with(prices(), models, ConversionStatus.PARTIAL_SUCCESS)
    result = partial.extract(make_pdf(RULED))
    assert result.outcome == "partial" and grid(result.tables[0]) == PRICES
    assert codes(result) == [("conversion_partial", None)]
    failed = extractor_with(prices(), models, ConversionStatus.FAILURE)
    result = failed.extract(make_pdf(RULED))
    assert result.outcome == "failed" and codes(result) == [("decode_failed", None)]


def test_passwords_are_applied_before_docling_sees_the_file(models):
    data = make_pdf(RULED, password="private-secret")
    for password, code in ((None, "password_required"), ("private-wrong", "invalid_password")):
        extractor = extractor_with(prices(), models)
        result = extractor.extract(Artifact(data=data, password=password))
        assert result.outcome == "failed" and codes(result) == [(code, None)]
        assert extractor._converter.calls == []
        assert "private" not in result.model_dump_json()
    extractor = extractor_with(prices(), models)
    result = extractor.extract(Artifact(data=data, password="private-secret"))
    assert result.outcome == "success"
    handed = extractor._converter.calls[-1][1]
    assert handed != data and b"/Encrypt" not in handed


@pytest.mark.parametrize(
    ("data", "outcome", "code"),
    [
        (b"", "failed", "decode_failed"),
        (b"plain text", "not_applicable", "not_applicable"),
        (b"%PDF-1.7 broken", "failed", "decode_failed"),
    ],
)
def test_bad_sources_never_reach_docling(models, data, outcome, code):
    extractor = extractor_with(prices(), models)
    result = extractor.extract(data)
    assert result.outcome == outcome and codes(result) == [(code, None)]
    assert extractor._converter.calls == []


@pytest.mark.parametrize(
    ("value", "error"), [(None, TypeError), (3, TypeError), ("/no/such/folder", ValueError)]
)
def test_models_path_is_validated_at_construction(value, error):
    with pytest.raises(error):
        DoclingTableExtractor(models_path=value)


def test_a_folder_without_models_is_a_configuration_error(models):
    with pytest.raises(ValueError, match="models_path does not contain usable Docling"):
        DoclingTableExtractor(models_path=models).extract(make_pdf(RULED))


def test_missing_docling_raises_an_install_instruction(monkeypatch, models):
    real_import = builtins.__import__

    def without_docling(name, *args, **kwargs):
        if name == "docling" or name.startswith("docling."):
            raise ImportError("no docling")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_docling)
    with pytest.raises(ImportError, match=r"raggae\[text-docling\]"):
        DoclingTableExtractor(models_path=models).extract(make_pdf(RULED))


def test_importing_the_package_imports_neither_docling_nor_img2table():
    code = (
        "import sys; import raggae.documents.table_extractor; "
        "assert not any(m.startswith(('docling', 'img2table', 'cv2')) for m in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_layouts_are_refused_before_the_source_is_opened(tmp_path, models):
    layout = make_layout(("grid", 1, "table", (0, 0, 10, 10)))
    result = extractor_with(prices(), models).extract(tmp_path / "missing.pdf", layout=layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "grid")
    ]


# Real pipeline (needs downloaded models) ------------------------------------------

needs_models = pytest.mark.skipif(
    not (MODELS / "docling-project--docling-models").is_dir(),
    reason="Docling models not downloaded (set RAGGAE_DOCLING_MODELS)",
)


@pytest.fixture(scope="module")
def real_extractor():
    return DoclingTableExtractor(models_path=MODELS)


@needs_models
def test_real_pipeline_offline(real_extractor, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    result = real_extractor.extract(make_pdf(shipments(), size=(595, 842)))
    assert result.outcome == "success"
    (table,) = result.tables
    rows = {}
    for item in table.cells:
        rows.setdefault(item.row, []).append(item.text)
    assert list(rows.values()) == [
        ["Item", "Qty", "Cost"],
        ["Shipping labels", "90", "45"],
        ["Packing tape", "18", "12"],
        ["Bubble wrap", "40", "30"],
    ]
    assert table.method == "model" and table.page_index == 0


@needs_models
def test_real_pipeline_merged_cell(real_extractor):
    (table,) = real_extractor.extract(make_pdf(shipments(merged=True), size=(595, 842))).tables
    assert (table.cells[0].text, table.cells[0].column_span) == ("Item and quantity", 2)
