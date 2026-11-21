"""Stored PDF text page by page with pypdf; whole pages only."""

from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
)
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    PdfplumberTextReader,
    PypdfTextReader,
    TextReader,
    TextReadResult,
)


def page(text, x=30, y=330):
    return f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET".encode()


def texts(result):
    return [block.text for block in result.blocks]


def pages(result):
    return [(block.page_index, block.text) for block in result.blocks]


def codes(result):
    return [finding.code for finding in result.findings]


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
                id="page-1-r1", surface_id="page-1", kind="text", bbox=(0, 0, 150, 400)
            ),
        ),
    )


def test_pdf_family_exports_the_same_worker():
    from raggae.documents.text_reader import pdf as pdf_readers

    assert pdf_readers.PypdfTextReader is PypdfTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, make_reader_pdf, source_kind, keyword
):
    data = make_reader_pdf((page("First page"), page("Second page")))
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = PypdfTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pypdf"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert pages(result) == [(0, "First page"), (1, "Second page")]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_plain_mode_lists_columns_and_layout_mode_keeps_them_side_by_side(columns_pdf):
    plain = PypdfTextReader().read(columns_pdf)
    assert pages(plain)[0] == (
        0,
        "Purchase report\nLeft one\nLeft two\nRight one\nRight two",
    )
    layout_mode = PypdfTextReader(mode="layout").read(columns_pdf)
    first = layout_mode.blocks[0].text.splitlines()
    assert any("Left one" in line and "Right one" in line for line in first)
    assert any("Left two" in line and "Right two" in line for line in first)
    assert pages(layout_mode)[1][1].strip() == "Follow-up note"


@pytest.mark.parametrize("mode", [None, 1, "fast"])
def test_mode_option_is_validated(mode):
    with pytest.raises((TypeError, ValueError)):
        PypdfTextReader(mode=mode)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_are_read(make_reader_pdf, rotation):
    result = PypdfTextReader().read(
        make_reader_pdf((page("Turned"),), rotation=rotation)
    )
    assert texts(result) == ["Turned"]


def test_text_outside_the_visible_area_is_kept_and_reported(make_reader_pdf):
    data = make_reader_pdf(
        (page("Inside") + b"\n" + page("Outside crop", y=30),), crop=[0, 200, 300, 400]
    )
    result = PypdfTextReader().read(data)
    assert result.outcome == "success"
    assert texts(result) == ["Inside\nOutside crop"]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("crop_not_applied", 0)
    ]
    # The pdfplumber reader leaves that text out.
    assert texts(PdfplumberTextReader().read(data)) == ["Inside"]


def test_full_page_crop_is_not_reported(make_reader_pdf):
    data = make_reader_pdf((page("Whole"),), crop=[0, 0, 300, 400])
    assert not PypdfTextReader().read(data).findings


def test_pages_without_stored_text_are_reported_empty(make_reader_pdf):
    data = make_reader_pdf(
        (page("Text page"), b"", b"q 2 0 0 2 30 30 cm /Im1 Do Q"), image=True
    )
    result = PypdfTextReader().read(data)
    assert result.outcome == "success"
    assert pages(result) == [(0, "Text page")]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("empty", 1),
        ("empty", 2),
    ]


def test_repeated_wording_is_not_deduplicated(make_reader_pdf):
    data = make_reader_pdf((page("Same"), page("Same")))
    assert texts(PypdfTextReader().read(data)) == ["Same", "Same"]


def test_unreadable_page_keeps_other_pages(make_reader_pdf, monkeypatch):
    from pypdf import PageObject
    from pypdf.errors import PdfReadError

    real = PageObject.extract_text
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise PdfReadError("private backend detail")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    data = make_reader_pdf((page("One"), page("Two"), page("Three")))
    result = PypdfTextReader().read(data)
    assert result.outcome == "partial"
    assert pages(result) == [(0, "One"), (2, "Three")]
    assert [(f.code, f.page_index) for f in result.findings] == [("page_unreadable", 1)]
    assert "private" not in result.model_dump_json()


def test_all_pages_unreadable_fails(make_reader_pdf, monkeypatch):
    from pypdf import PageObject
    from pypdf.errors import PdfReadError

    def broken(self, *args, **kwargs):
        raise PdfReadError("broken")

    monkeypatch.setattr(PageObject, "extract_text", broken)
    result = PypdfTextReader().read(make_reader_pdf((page("One"),)))
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == ["page_unreadable"]


# Passwords ---------------------------------------------------------------


def test_empty_user_password_opens_without_a_password(make_reader_pdf):
    data = make_reader_pdf((page("Open"),), password="")
    assert texts(PypdfTextReader().read(data)) == ["Open"]


def test_correct_password_opens(make_reader_pdf):
    data = make_reader_pdf((page("Secret"),), password="private-pass")
    result = PypdfTextReader().read(Artifact(data=data, password="private-pass"))
    assert texts(result) == ["Secret"]
    assert "private-pass" not in result.model_dump_json()


@pytest.mark.parametrize(
    "given,code", [(None, "password_required"), ("wrong-private", "invalid_password")]
)
def test_missing_or_wrong_password_fails_safely(make_reader_pdf, given, code):
    data = make_reader_pdf((page("Secret"),), password="private-pass")
    result = PypdfTextReader().read(Artifact(data=data, password=given))
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == [code]
    dumped = result.model_dump_json()
    assert "private" not in dumped and "Secret" not in dumped


# Invalid sources -----------------------------------------------------------


def test_non_pdf_is_not_applicable():
    result = PypdfTextReader().read(b"private plain text")
    assert result.outcome == "not_applicable" and not result.blocks
    assert codes(result) == ["not_applicable"]
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "decode_failed"),
        (b"%PDF-1.7\nprivate garbage", "decode_failed"),
    ],
)
def test_empty_or_broken_pdf_fails_safely(data, code):
    result = PypdfTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert codes(result) == [code]
    assert "private" not in result.model_dump_json()


def test_pdf_without_pages_fails(monkeypatch, make_reader_pdf):
    from pypdf import PdfReader

    monkeypatch.setattr(PdfReader, "pages", property(lambda self: []))
    result = PypdfTextReader().read(make_reader_pdf())
    assert codes(result) == ["no_pages"]


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = PypdfTextReader()
    result = reader.read(tmp_path / "private-name.pdf")
    assert codes(result) == ["source_unreadable"]
    assert "private-name" not in result.model_dump_json()
    assert reader.read(tmp_path).outcome == "failed"
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda self: SimpleNamespace(st_mode=S_IFIFO)
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open a FIFO")
    )
    assert reader.read(tmp_path / "pipe").outcome == "failed"


# Layout regions ------------------------------------------------------------


@pytest.mark.parametrize("selection", [None, ["page-1-r1"]])
def test_layout_regions_are_rejected_before_source_io(
    tmp_path, monkeypatch, layout, selection
):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = PypdfTextReader().read(
        tmp_path / "missing.pdf", layout=layout, region_ids=selection
    )
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "page-1-r1")
    ]
    assert "PdfplumberTextReader" in result.findings[0].message


def test_a_layout_is_refused_even_when_nothing_is_selected_and_arguments_are_validated(
    tmp_path, layout
):
    reader = PypdfTextReader()
    source = tmp_path / "missing.pdf"
    result = reader.read(source, layout=layout, region_ids=[])
    assert result.outcome == "failed" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", None)
    ]
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=layout, region_ids=["unknown"])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")


def test_streams_are_not_silently_added_to_source_contract(make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        PypdfTextReader().read(BytesIO(make_reader_pdf()))
