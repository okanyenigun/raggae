"""Native page extraction and exact region selection through the common API."""

import builtins
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
    NativePdfLayoutAnalyzer,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    PdfplumberTextReader,
    TextReader,
    TextReadResult,
)


@pytest.fixture
def backend():
    return pytest.importorskip("pdfplumber", reason="Install raggae[text-pdf]")


@pytest.fixture
def column_layout():
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
                id="left", surface_id="page-1", kind="text", bbox=(20, 50, 150, 100)
            ),
            LayoutRegion(
                id="right", surface_id="page-1", kind="text", bbox=(170, 50, 280, 100)
            ),
        ),
    )


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [True, False])
def test_whole_pdf_sources_and_result_shape(
    backend, columns_pdf, tmp_path, source_kind, keyword
):
    path = tmp_path / "source.bin"
    path.write_bytes(columns_pdf)
    sources = {
        "path": path,
        "string": str(path),
        "bytes": columns_pdf,
        "artifact_path": Artifact(path=path),
        "artifact_bytes": Artifact(data=columns_pdf),
    }
    reader = PdfplumberTextReader()
    source = sources[source_kind]
    result = reader.read(source=source) if keyword else reader.read(source)
    assert isinstance(reader, TextReader)
    assert reader.name == result.reader == "text_reader_pdfplumber"
    assert result.outcome == "success" and not result.findings
    assert len(result.blocks) == 2
    assert "Left one" in result.blocks[0].text and "Right one" in result.blocks[0].text
    assert result.blocks[1].text == "Follow-up note"
    assert [b.page_index for b in result.blocks] == [0, 1]
    for block in result.blocks:
        assert "location" not in block.model_dump()
        assert "bbox" not in block.model_dump()
        assert block.region_id is None
        assert block.content_format == "plain_text" and block.method == "native"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert path.read_bytes() == columns_pdf
    assert list(tmp_path.iterdir()) == [path]


def test_selecting_one_column_excludes_other_column_and_heading(
    backend, columns_pdf, column_layout
):
    result = PdfplumberTextReader().read(
        columns_pdf, layout=column_layout, region_ids=("left",)
    )
    assert result.outcome == "success" and not result.findings
    (block,) = result.blocks
    assert block.text == "Left one\nLeft two"
    assert block.region_id == "left"
    assert block.page_index == 0
    assert "Right" not in block.text and "Purchase" not in block.text


def test_all_regions_and_requested_sequence_within_page(
    backend, columns_pdf, column_layout
):
    reader = PdfplumberTextReader()
    result = reader.read(columns_pdf, layout=column_layout)
    assert [b.region_id for b in result.blocks] == ["left", "right"]
    assert [b.text for b in result.blocks] == [
        "Left one\nLeft two",
        "Right one\nRight two",
    ]
    reversed_result = reader.read(
        columns_pdf, layout=column_layout, region_ids=["right", "left"]
    )
    assert [b.region_id for b in reversed_result.blocks] == ["right", "left"]


def test_real_layout_analyzer_output_is_consumed_without_adapters(backend, columns_pdf):
    layout = NativePdfLayoutAnalyzer().analyze(columns_pdf)
    assert layout.outcome == "success"
    result = PdfplumberTextReader().read(columns_pdf, layout=layout)
    assert result.outcome == "success"
    assert {b.text for b in result.blocks} == {
        "Purchase report",
        "Left one\nLeft two",
        "Right one\nRight two",
        "Follow-up note",
    }
    assert {b.region_id for b in result.blocks} == {r.id for r in layout.regions}
    regions = {r.id: r for r in layout.regions}
    pages = {s.id_: s.location.page_number - 1 for s in layout.surfaces}
    for block in result.blocks:
        assert block.page_index == pages[regions[block.region_id].surface_id]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("origin", [(0, 0), (10, 20)])
@pytest.mark.parametrize("unit", [1, 2])
def test_layout_coordinates_survive_crop_rotation_origin_and_userunit(
    backend,
    make_reader_pdf,
    rotation,
    origin,
    unit,
):
    dx, dy = origin
    content = (
        f"BT /F1 12 Tf {50 + dx} {300 + dy} Td (Visible) Tj ET\n"
        f"BT /F1 12 Tf {50 + dx} {390 + dy} Td (Outside) Tj ET"
    ).encode()
    data = make_reader_pdf(
        (content,),
        rotation=rotation,
        unit=unit,
        media=(dx, dy, 300 + dx, 400 + dy),
        crop=(20 + dx, 30 + dy, 260 + dx, 350 + dy),
    )
    layout = NativePdfLayoutAnalyzer().analyze(data)
    assert layout.outcome == "success" and len(layout.regions) == 1
    reader = PdfplumberTextReader()
    for result in (reader.read(data), reader.read(data, layout=layout)):
        assert result.outcome == "success"
        (block,) = result.blocks
        assert block.text == "Visible"
        assert block.page_index == 0
    selected = reader.read(data, layout=layout)
    assert selected.blocks[0].region_id == layout.regions[0].id
    assert selected.blocks[0].page_index == 0


def test_overlapping_regions_are_reported_without_duplicate_text(
    backend, columns_pdf, column_layout
):
    duplicate = LayoutRegion(
        id="duplicate", surface_id="page-1", kind="text", bbox=(20, 50, 150, 100)
    )
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=column_layout.surfaces,
        regions=column_layout.regions + (duplicate,),
    )
    result = PdfplumberTextReader().read(columns_pdf, layout=layout)
    assert result.outcome == "partial"
    assert [b.region_id for b in result.blocks] == ["right"]
    assert {f.region_id for f in result.findings} == {"left", "duplicate"}
    assert all(f.code == "selection_overlap" for f in result.findings)
    all_overlapping = PdfplumberTextReader().read(
        columns_pdf, layout=layout, region_ids=("left", "duplicate")
    )
    assert all_overlapping.outcome == "failed" and not all_overlapping.blocks


def test_repeated_wording_at_different_locations_is_not_deduplicated(
    backend, make_reader_pdf, column_layout
):
    data = make_reader_pdf(
        (b"BT /F1 12 Tf 30 330 Td (Same) Tj ET BT /F1 12 Tf 180 330 Td (Same) Tj ET",)
    )
    result = PdfplumberTextReader().read(data, layout=column_layout)
    assert result.outcome == "success"
    assert [b.text for b in result.blocks] == ["Same", "Same"]


@pytest.mark.parametrize("image", [False, True])
def test_no_native_text_is_explicit_without_claiming_visual_blankness(
    backend, make_reader_pdf, image
):
    content = b"q 200 0 0 300 30 40 cm /Im1 Do Q" if image else b""
    result = PdfplumberTextReader().read(make_reader_pdf((content,), image=image))
    assert result.outcome == "success" and not result.blocks
    (finding,) = result.findings
    assert finding.code == "empty"
    assert finding.page_index == 0
    assert "pixels" in finding.message


def test_empty_native_region_has_its_own_finding(
    backend, make_reader_pdf, column_layout
):
    result = PdfplumberTextReader().read(make_reader_pdf((b"",)), layout=column_layout)
    assert result.outcome == "success" and not result.blocks
    assert {f.region_id for f in result.findings} == {"left", "right"}
    assert all(f.code == "empty" for f in result.findings)
    assert all(f.page_index == 0 for f in result.findings)


def test_image_region_is_unsupported_while_text_region_succeeds(
    backend, columns_pdf, column_layout
):
    image = LayoutRegion(
        id="image", surface_id="page-1", kind="image", bbox=(0, 0, 10, 10)
    )
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=column_layout.surfaces,
        regions=(column_layout.regions[0], image),
    )
    result = PdfplumberTextReader().read(columns_pdf, layout=layout)
    assert result.outcome == "partial" and result.blocks[0].region_id == "left"
    assert result.findings[0].region_id == "image"
    assert result.findings[0].code == "selection_unsupported"
    assert result.findings[0].page_index == 0


def test_wrong_surface_dimensions_are_not_silently_accepted(
    backend, columns_pdf, column_layout
):
    surface = LayoutSurface(
        id_="page-1", location=PdfPageLocation(page_number=1), width=600, height=800
    )
    layout = LayoutAnalysisResult(
        outcome="success", surfaces=(surface,), regions=column_layout.regions
    )
    result = PdfplumberTextReader().read(columns_pdf, layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert {f.region_id for f in result.findings} == {"left", "right"}
    assert all(f.code == "layout_mismatch" for f in result.findings)


def test_missing_source_page_is_reported_per_region(backend, make_reader_pdf):
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-5",
                location=PdfPageLocation(page_number=5),
                width=300,
                height=400,
            ),
        ),
        regions=(
            LayoutRegion(
                id="missing", surface_id="page-5", kind="text", bbox=(20, 30, 100, 100)
            ),
        ),
    )
    result = PdfplumberTextReader().read(make_reader_pdf(), layout=layout)
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].region_id == "missing"
    assert result.findings[0].code == "page_missing"
    assert result.findings[0].page_index == 4


@pytest.mark.parametrize(
    "password,code",
    [(None, "password_required"), ("private-wrong", "invalid_password")],
)
def test_password_errors_are_safe(backend, make_reader_pdf, password, code):
    data = make_reader_pdf(password="private-secret")
    result = PdfplumberTextReader().read(Artifact(data=data, password=password))
    assert result.outcome == "failed" and not result.blocks
    assert result.findings[0].code == code
    assert "private-" not in result.model_dump_json()


def test_password_is_taken_from_artifact_and_source_is_unchanged(
    backend, make_reader_pdf, tmp_path
):
    data = make_reader_pdf(password="reader-demo-secret")
    path = tmp_path / "protected.pdf"
    path.write_bytes(data)
    source = Artifact(path=path, password="reader-demo-secret")
    result = PdfplumberTextReader().read(source)
    assert result.outcome == "success" and result.blocks[0].text == "Hello reader"
    assert path.read_bytes() == data and source.password == "reader-demo-secret"
    assert "reader-demo-secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,outcome,code",
    [
        (b"", "failed", "decode_failed"),
        (b"%PDF-1.7\nbroken", "failed", "decode_failed"),
        (b"Hello", "not_applicable", "not_applicable"),
    ],
)
def test_bad_sources(backend, data, outcome, code):
    result = PdfplumberTextReader().read(data)
    assert result.outcome == outcome and not result.blocks
    assert result.findings[0].code == code
    assert result.findings[0].page_index is None


def test_file_is_read_without_an_automatic_size_cap(
    backend,
    make_reader_pdf,
    tmp_path,
    monkeypatch,
):
    data = make_reader_pdf()
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    # Report a large size without allocating a large PDF just for this IO test.
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *args, **kwargs: SimpleNamespace(
            st_mode=S_IFREG,
            st_size=60 * 1024 * 1024,
        ),
    )
    sizes = []

    class RecordingStream(BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    stream = RecordingStream(data)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    result = PdfplumberTextReader().read(path)
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == "Hello reader"
    assert sizes == [-1] and stream.closed


@pytest.mark.parametrize("selected", [False, True])
def test_pages_beyond_the_previous_default_are_read(
    backend,
    make_reader_pdf,
    selected,
):
    data = make_reader_pdf(
        (b"",) * 100 + (b"BT /F1 12 Tf 30 330 Td (Last page) Tj ET",)
    )
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-101",
                location=PdfPageLocation(page_number=101),
                width=300,
                height=400,
            ),
        ),
        regions=(
            LayoutRegion(
                id="last",
                surface_id="page-101",
                kind="text",
                bbox=(20, 50, 150, 100),
            ),
        ),
    )
    result = PdfplumberTextReader().read(data, layout=layout if selected else None)
    assert result.outcome == "success"
    (block,) = result.blocks
    assert block.text == "Last page" and block.page_index == 100
    assert block.region_id == ("last" if selected else None)
    assert all(f.code == "empty" for f in result.findings)


def test_backend_text_is_returned_without_a_reader_output_cap(
    backend,
    make_reader_pdf,
    monkeypatch,
):
    import pdfplumber.utils.text as pdf_text

    # Isolate result handling from PDF parsing; the backend supplies this text.
    text = "x" * 10_000_001
    monkeypatch.setattr(pdf_text, "extract_text", lambda chars: text)
    result = PdfplumberTextReader().read(make_reader_pdf())
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == text


def test_reader_does_not_revalidate_native_character_count(
    backend,
    make_reader_pdf,
    monkeypatch,
):
    from pdfplumber.page import Page

    original = Page.chars.fget

    class LargeCharacterList(list):
        def __len__(self):
            return 100_001

    # Exercise the previous threshold without allocating 100,001 glyph objects.
    monkeypatch.setattr(
        Page, "chars", property(lambda page: LargeCharacterList(original(page)))
    )
    result = PdfplumberTextReader().read(make_reader_pdf())
    assert result.outcome == "success" and not result.findings
    assert result.blocks[0].text == "Hello reader"


def test_page_failure_keeps_other_pages_and_hides_parser_errors(
    backend, columns_pdf, monkeypatch
):
    from pdfplumber.page import Page
    from pdfplumber.utils.exceptions import PdfminerException

    original = Page.parse_objects

    def broken(self):
        if self.page_number == 1:
            raise PdfminerException("private document details")
        return original(self)

    monkeypatch.setattr(Page, "parse_objects", broken)
    result = PdfplumberTextReader().read(columns_pdf)
    assert result.outcome == "partial"
    assert [b.page_index for b in result.blocks] == [1]
    assert result.findings[0].page_index == 0
    assert result.findings[0].code == "page_unreadable"
    assert "private" not in result.model_dump_json()


def test_stream_is_closed_without_materializing_pdf_pages(
    backend, make_reader_pdf, monkeypatch
):
    from pdfplumber.pdf import PDF

    import raggae.documents.text_reader.pdf.plumber as module

    streams = []

    class RecordedStream(BytesIO):
        def __init__(self, data):
            super().__init__(data)
            streams.append(self)

    def forbidden(self):
        raise AssertionError("Must not materialize PDF.pages")

    monkeypatch.setattr(module, "BytesIO", RecordedStream)
    monkeypatch.setattr(PDF, "pages", property(forbidden))
    assert PdfplumberTextReader().read(make_reader_pdf()).outcome == "success"
    assert streams and all(stream.closed for stream in streams)


def test_missing_backend_raises_install_instruction(make_reader_pdf, monkeypatch):
    data = make_reader_pdf()
    original = builtins.__import__

    def without_backend(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("backend unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_backend)
    with pytest.raises(ImportError, match=r"raggae\[text-pdf\]"):
        PdfplumberTextReader().read(data)


def test_empty_selection_says_nothing_was_selected_without_touching_source_or_backend(
    tmp_path, column_layout
):
    result = PdfplumberTextReader().read(
        tmp_path / "missing.pdf", layout=column_layout, region_ids=()
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


@pytest.mark.parametrize(
    "ids,error",
    [
        ("left", TypeError),
        ([None], TypeError),
        (("left", "left"), ValueError),
        (("missing",), ValueError),
    ],
)
def test_invalid_selection_is_a_caller_error(column_layout, ids, error):
    with pytest.raises(error):
        PdfplumberTextReader().read(b"pdf", layout=column_layout, region_ids=ids)


def test_invalid_arguments_are_not_document_failures():
    reader = PdfplumberTextReader()
    for source in (None, 123, BytesIO(b"pdf")):
        with pytest.raises(TypeError):
            reader.read(source)
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(b"pdf", region_ids=())
    with pytest.raises(TypeError, match="layout must be"):
        reader.read(b"pdf", layout={})
    with pytest.raises(TypeError):
        PdfplumberTextReader(policy={})
    unusable = LayoutAnalysisResult(
        outcome="failed", findings=(LayoutFinding(code="failed", message="Failed."),)
    )
    with pytest.raises(ValueError, match="usable map"):
        reader.read(b"pdf", layout=unusable)


def test_pdf_reader_has_no_policy_api():
    import raggae.documents.text_reader as public
    import raggae.documents.text_reader.pdf as pdf

    assert not hasattr(PdfplumberTextReader(), "policy")
    assert not hasattr(public, "PdfTextPolicy")
    assert not hasattr(pdf, "PdfTextPolicy")
