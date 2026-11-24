"""GraniteDoclingTextReader: Docling's VLM pipeline with granite-docling-258M, offline."""

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.text_reader import (
    DoclingPdfTextReader,
    GraniteDoclingTextReader,
    TextReader,
    TextReadResult,
)
from raggae.documents.text_reader.pdf import GraniteDoclingTextReader as FromPdf

pytest.importorskip("docling", reason="Install raggae[text-docling]")

from docling.datamodel.base_models import ConversionStatus  # noqa: E402
from docling_core.types.doc import (  # noqa: E402
    BoundingBox,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)

FOLDER = "ibm-granite--granite-docling-258M"


def prov(page):
    return ProvenanceItem(
        page_no=page, bbox=BoundingBox(l=10, t=10, r=100, b=30), charspan=(0, 1)
    )


def document(pages=2):
    doc = DoclingDocument(name="source")
    for number in range(1, pages + 1):
        doc.add_page(page_no=number, size=Size(width=300, height=400))
    return doc


class FakeConverter:
    """Stands in for Docling's VLM converter; returns a prepared document."""

    def __init__(self, doc, status=ConversionStatus.SUCCESS, errors=()):
        self.doc, self.status, self.errors, self.calls = doc, status, errors, []

    def convert(self, source, *, raises_on_error):
        self.calls.append((source.name, source.stream.getvalue(), raises_on_error))
        return SimpleNamespace(
            status=self.status, document=self.doc, errors=list(self.errors)
        )


def page_error(page_no):
    return SimpleNamespace(page_no=page_no, error_message="private backend detail")


@pytest.fixture
def models(tmp_path):
    folder = tmp_path / "models" / FOLDER
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    return tmp_path / "models"


def reader_with(doc, models, status=ConversionStatus.SUCCESS, errors=()):
    reader = GraniteDoclingTextReader(models_path=models)
    reader._converter = FakeConverter(doc, status, errors)
    return reader


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


# Contract (stand-in converter) ------------------------------------------------


def test_elements_become_vision_blocks_with_pages_and_markdown_tables(
    models, make_reader_pdf
):
    doc = document(2)
    doc.add_heading(text="Purchase report", prov=prov(1))
    doc.add_text(label=DocItemLabel.TEXT, text="Shipping labels", prov=prov(1))
    cells = [
        TableCell(
            text=text,
            start_row_offset_idx=r,
            end_row_offset_idx=r + 1,
            start_col_offset_idx=c,
            end_col_offset_idx=c + 1,
            column_header=r == 0,
        )
        for r, row in enumerate([["Item", "Cost"], ["Tape", "18"]])
        for c, text in enumerate(row)
    ]
    doc.add_table(
        data=TableData(num_rows=2, num_cols=2, table_cells=cells), prov=prov(2)
    )
    data = make_reader_pdf((b"", b""))
    reader = reader_with(doc, models)
    assert isinstance(reader, TextReader) and FromPdf is GraniteDoclingTextReader
    assert isinstance(reader, DoclingPdfTextReader)
    assert reader.name == "text_reader_granite_docling"
    result = reader.read(data)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, b.content_format, b.method) for b in result.blocks] == [
        (0, "plain_text", "vision"),
        (0, "plain_text", "vision"),
        (1, "markdown", "vision"),
    ]
    assert [b.text for b in result.blocks[:2]] == ["Purchase report", "Shipping labels"]
    assert "| Item" in result.blocks[2].text
    assert result.reader == "text_reader_granite_docling"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert reader._converter.calls[0][1:] == (data, False)


def test_empty_pages_and_pictures_are_reported_in_the_readers_terms(
    models, make_reader_pdf
):
    doc = document(2)
    doc.add_picture(prov=prov(1))
    result = reader_with(doc, models).read(make_reader_pdf((b"", b"")))
    assert result.outcome == "success" and not result.blocks
    assert codes(result) == [
        ("pictures_unread", 0),
        ("empty", 0),
        ("empty", 1),
    ]
    assert "did not transcribe" in result.findings[0].message
    assert "OCR is off" not in result.model_dump_json()


def test_unfinished_pages_are_left_out_and_reported(models, make_reader_pdf):
    doc = document(3)
    doc.add_text(label=DocItemLabel.TEXT, text="Page one", prov=prov(1))
    doc.add_text(label=DocItemLabel.TEXT, text="Cut short", prov=prov(2))
    doc.add_text(label=DocItemLabel.TEXT, text="Page three", prov=prov(3))
    result = reader_with(
        doc, models, ConversionStatus.PARTIAL_SUCCESS, [page_error(2)]
    ).read(make_reader_pdf((b"", b"", b"")))
    assert result.outcome == "partial"
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (0, "Page one"),
        (2, "Page three"),
    ]
    assert codes(result) == [("page_incomplete", 1)]
    assert "private" not in result.model_dump_json()


def test_unfinished_empty_page_is_reported_once(models, make_reader_pdf):
    result = reader_with(
        document(1), models, ConversionStatus.PARTIAL_SUCCESS, [page_error(1)]
    ).read(make_reader_pdf())
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == [("page_incomplete", 0)]


def test_document_level_errors_keep_content_and_say_so(models, make_reader_pdf):
    doc = document(1)
    doc.add_text(label=DocItemLabel.TEXT, text="Kept", prov=prov(1))
    result = reader_with(
        doc, models, ConversionStatus.PARTIAL_SUCCESS, [page_error(None)]
    ).read(make_reader_pdf())
    assert result.outcome == "partial"
    assert [b.text for b in result.blocks] == ["Kept"]
    assert codes(result) == [("conversion_partial", None)]


@pytest.mark.parametrize("status", [ConversionStatus.FAILURE, ConversionStatus.SKIPPED])
def test_failed_conversion_is_decode_failed(models, make_reader_pdf, status):
    result = reader_with(document(1), models, status).read(make_reader_pdf())
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == [("decode_failed", None)]


def test_sources_passwords_and_non_pdf(models, make_reader_pdf):
    from raggae.documents.normalization import Artifact

    doc = document(1)
    doc.add_text(label=DocItemLabel.TEXT, text="Secret", prov=prov(1))
    data = make_reader_pdf(password="secret")
    reader = reader_with(doc, models)
    assert codes(reader.read(data)) == [("password_required", None)]
    assert [
        b.text for b in reader.read(Artifact(data=data, password="secret")).blocks
    ] == ["Secret"]
    assert reader.read(b"plain text").outcome == "not_applicable"
    assert len(reader._converter.calls) == 1


def test_layout_regions_are_rejected(models, make_reader_pdf):
    data = make_reader_pdf()
    layout = NativePdfLayoutAnalyzer().analyze(data)
    result = reader_with(document(1), models).read(data, layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.code for f in result.findings} == {"selection_unsupported"}
    assert (
        "GraniteDoclingTextReader reads whole documents" in result.findings[0].message
    )


# Configuration --------------------------------------------------------------


@pytest.mark.parametrize(
    "value,error", [(None, TypeError), (3, TypeError), ("/no/such/folder", ValueError)]
)
def test_models_path_is_validated_at_construction(value, error):
    with pytest.raises(error):
        GraniteDoclingTextReader(models_path=value)


def test_a_folder_without_the_model_is_rejected_and_never_downloaded(tmp_path):
    (tmp_path / "docling-project--docling-models").mkdir()
    with pytest.raises(ValueError, match=f"{FOLDER}.*never downloaded"):
        GraniteDoclingTextReader(models_path=tmp_path)


@pytest.mark.parametrize(
    "repo",
    ["", "granite", "a/b/c", "owner/", "/name", "own er/name", "../x/y z", None, 3],
)
def test_model_repo_is_validated(models, repo):
    with pytest.raises((ValueError, TypeError)):
        GraniteDoclingTextReader(models_path=models, model_repo=repo)  # type: ignore[arg-type]


def test_another_model_repo_is_looked_up_in_its_own_folder(tmp_path):
    folder = tmp_path / "models" / "docling-project--SmolDocling-256M-preview"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    reader = GraniteDoclingTextReader(
        models_path=tmp_path / "models",
        model_repo="docling-project/SmolDocling-256M-preview",
    )
    assert reader._model_repo == "docling-project/SmolDocling-256M-preview"
    with pytest.raises(ValueError, match=f"{FOLDER}.*never downloaded"):
        GraniteDoclingTextReader(models_path=tmp_path / "models")


def test_models_path_is_required():
    with pytest.raises(TypeError):
        GraniteDoclingTextReader()  # type: ignore[call-arg]


def test_pipeline_is_offline_vision_only_transformers(models, monkeypatch):
    from docling.datamodel.pipeline_options_vlm_model import InferenceFramework
    from docling.document_converter import DocumentConverter
    from docling.pipeline.vlm_pipeline import VlmPipeline

    seen = []
    monkeypatch.setattr(
        DocumentConverter, "initialize_pipeline", lambda self, fmt: seen.append(self)
    )
    reader = GraniteDoclingTextReader(models_path=models)
    converter = reader._load()
    assert reader._load() is converter and len(seen) == 1
    (option,) = converter.format_to_options.values()
    assert option.pipeline_cls is VlmPipeline
    options = option.pipeline_options
    assert Path(options.artifacts_path) == models.resolve()
    assert options.force_backend_text is False
    assert options.enable_remote_services is False
    assert options.generate_picture_images is False
    vlm = options.vlm_options
    assert vlm.repo_id == "ibm-granite/granite-docling-258M"
    assert vlm.inference_framework == InferenceFramework.TRANSFORMERS
    assert vlm.trust_remote_code is False and vlm.temperature == 0.0


def test_unusable_model_folder_is_a_configuration_error(models, monkeypatch):
    from docling.document_converter import DocumentConverter

    def broken(self, fmt):
        raise OSError("private backend detail")

    monkeypatch.setattr(DocumentConverter, "initialize_pipeline", broken)
    with pytest.raises(ValueError, match="usable ibm-granite/granite-docling-258M"):
        GraniteDoclingTextReader(models_path=models)._load()


def test_missing_backend_has_an_install_hint(monkeypatch, models, make_reader_pdf):
    real_import = builtins.__import__

    def without_docling(name, *args, **kwargs):
        if name == "docling" or name.startswith("docling."):
            raise ImportError("no docling")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_docling)
    with pytest.raises(ImportError, match=r"raggae\[text-docling\]"):
        GraniteDoclingTextReader(models_path=models).read(make_reader_pdf())


def test_the_chosen_model_repo_is_what_the_pipeline_loads(tmp_path, monkeypatch):
    from docling.document_converter import DocumentConverter

    folder = tmp_path / "models" / "docling-project--SmolDocling-256M-preview"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    monkeypatch.setattr(
        DocumentConverter, "initialize_pipeline", lambda self, fmt: None
    )
    reader = GraniteDoclingTextReader(
        models_path=tmp_path / "models",
        model_repo="docling-project/SmolDocling-256M-preview",
    )
    (option,) = reader._load().format_to_options.values()
    assert (
        option.pipeline_options.vlm_options.repo_id
        == "docling-project/SmolDocling-256M-preview"
    )
