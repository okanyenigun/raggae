"""pdfminer text boxes by page or region, selecting characters by centre first."""

import builtins
from io import BytesIO
from stat import S_IFIFO
from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.layout_analysis import (
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
    NativePdfLayoutAnalyzer,
)
from raggae.documents.layout_analysis.models import DocxBodyLocation, PdfPageLocation
from raggae.documents.normalization import Artifact
from raggae.documents.text_reader import (
    PdfminerTextReader,
    PdfplumberTextReader,
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
    return pytest.importorskip("pdfminer", reason="Install raggae[text-pdf]")


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


def text_page(text, x=30, y=330):
    return f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET".encode()


def summary(result):
    return (
        result.outcome,
        [(b.page_index, b.region_id, b.text) for b in result.blocks],
        [(f.code, f.region_id, f.page_index) for f in result.findings],
    )


def test_pdf_family_exports_the_same_worker():
    from raggae.documents.text_reader import pdf as pdf_readers

    assert pdf_readers.PdfminerTextReader is PdfminerTextReader


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
@pytest.mark.parametrize("keyword", [False, True])
def test_sources_schema_protocol_and_unchanged_file(
    tmp_path, make_reader_pdf, source_kind, keyword
):
    data = make_reader_pdf((REPORT, text_page("Follow-up note")))
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    reader = PdfminerTextReader()
    assert isinstance(reader, TextReader)
    assert reader.name == "text_reader_pdfminer"
    result = reader.read(source=source) if keyword else reader.read(source)
    assert result.outcome == "success" and not result.findings
    assert [(b.page_index, b.text) for b in result.blocks] == [
        (
            0,
            "Purchase report\n\nShipping labels\nCost: 90 USD\n\nPacking tape\nCost: 18 USD",
        ),
        (1, "Follow-up note"),
    ]
    for block in result.blocks:
        assert block.region_id is None
        assert block.method == "native" and block.content_format == "plain_text"
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]
    path.unlink()


def test_region_reads_group_only_the_selected_characters(make_reader_pdf):
    data = make_reader_pdf((REPORT,))
    layout = NativePdfLayoutAnalyzer().analyze(data)
    result = PdfminerTextReader().read(data, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert [b.text for b in result.blocks] == [
        "Purchase report",
        "Shipping labels\nCost: 90 USD",
        "Packing tape\nCost: 18 USD",
    ]
    assert summary(result) == summary(PdfplumberTextReader().read(data, layout=layout))


def test_one_region_spanning_two_boxes_separates_them(make_reader_pdf, column_layout):
    data = make_reader_pdf((REPORT,))
    wide = LayoutAnalysisResult(
        outcome="success",
        surfaces=column_layout.surfaces,
        regions=(
            LayoutRegion(
                id="both", surface_id="page-1", kind="text", bbox=(20, 50, 280, 100)
            ),
        ),
    )
    result = PdfminerTextReader().read(data, layout=wide)
    assert [b.text for b in result.blocks] == [
        "Shipping labels\nCost: 90 USD\n\nPacking tape\nCost: 18 USD"
    ]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("origin", [(0, 0), (10, 20)])
@pytest.mark.parametrize("unit", [1, 2])
def test_crop_rotation_origin_and_userunit_match_pdfplumber(
    make_reader_pdf, rotation, origin, unit
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
    reader, plumber = PdfminerTextReader(), PdfplumberTextReader()
    whole, selected = reader.read(data), reader.read(data, layout=layout)
    assert [b.text for b in whole.blocks] == ["Visible"]
    assert [(b.region_id, b.text) for b in selected.blocks] == [
        (layout.regions[0].id, "Visible")
    ]
    assert summary(whole) == summary(plumber.read(data))
    assert summary(selected) == summary(plumber.read(data, layout=layout))


def test_rotated_page_keeps_box_order(make_reader_pdf):
    data = make_reader_pdf((REPORT,), rotation=180)
    assert [b.text for b in PdfminerTextReader().read(data).blocks] == [
        "Purchase report\n\nShipping labels\nCost: 90 USD\n\nPacking tape\nCost: 18 USD"
    ]


def test_overlapping_regions_are_both_rejected_like_pdfplumber(
    make_reader_pdf, column_layout
):
    data = make_reader_pdf((REPORT,))
    duplicate = LayoutRegion(
        id="duplicate", surface_id="page-1", kind="text", bbox=(20, 50, 150, 100)
    )
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=column_layout.surfaces,
        regions=column_layout.regions + (duplicate,),
    )
    result = PdfminerTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert [b.region_id for b in result.blocks] == ["right"]
    assert {f.region_id for f in result.findings} == {"left", "duplicate"}
    assert all(f.code == "selection_overlap" for f in result.findings)
    assert summary(result) == summary(PdfplumberTextReader().read(data, layout=layout))


def test_text_outside_the_visible_area_is_left_out(make_reader_pdf):
    data = make_reader_pdf(
        (text_page("Inside") + b"\n" + text_page("Outside crop", y=30),),
        crop=[0, 200, 300, 400],
    )
    result = PdfminerTextReader().read(data)
    assert result.outcome == "success" and not result.findings
    assert [b.text for b in result.blocks] == ["Inside"]


def test_text_inside_form_xobjects_is_read():
    with pikepdf.Pdf.new() as document, BytesIO() as output:
        page = document.add_blank_page(page_size=(300, 400))
        font = document.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
            )
        )
        form = document.make_stream(text_page("Inside a form"))
        form.Type = pikepdf.Name.XObject
        form.Subtype = pikepdf.Name.Form
        form.BBox = pikepdf.Array([0, 0, 300, 400])
        form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm1=form))
        page.Contents = document.make_stream(b"q /Fm1 Do Q")
        document.save(output)
        data = output.getvalue()
    assert [b.text for b in PdfminerTextReader().read(data).blocks] == ["Inside a form"]


def test_pages_without_stored_text_are_reported_empty(make_reader_pdf):
    data = make_reader_pdf(
        (text_page("Text page"), b"", b"q 2 0 0 2 30 30 cm /Im1 Do Q"), image=True
    )
    result = PdfminerTextReader().read(data)
    assert result.outcome == "success"
    assert [(b.page_index, b.text) for b in result.blocks] == [(0, "Text page")]
    assert [(f.code, f.page_index) for f in result.findings] == [
        ("empty", 1),
        ("empty", 2),
    ]


def test_repeated_wording_is_not_deduplicated(make_reader_pdf):
    data = make_reader_pdf((text_page("Same"), text_page("Same")))
    assert [b.text for b in PdfminerTextReader().read(data).blocks] == ["Same", "Same"]


def test_image_regions_non_pdf_surfaces_missing_pages_and_mismatched_sizes(
    make_reader_pdf,
):
    data = make_reader_pdf((REPORT, text_page("Two")))
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="page-1",
                location=PdfPageLocation(page_number=1),
                width=300,
                height=400,
            ),
            LayoutSurface(
                id_="page-9",
                location=PdfPageLocation(page_number=9),
                width=300,
                height=400,
            ),
            LayoutSurface(
                id_="wrong-size",
                location=PdfPageLocation(page_number=2),
                width=600,
                height=400,
            ),
            LayoutSurface(
                id_="body",
                kind="flow",
                location=DocxBodyLocation(part="word/document.xml"),
            ),
        ),
        regions=(
            LayoutRegion(
                id="text", surface_id="page-1", kind="text", bbox=(20, 50, 150, 100)
            ),
            LayoutRegion(
                id="picture", surface_id="page-1", kind="image", bbox=(0, 0, 10, 10)
            ),
            LayoutRegion(
                id="far", surface_id="page-9", kind="text", bbox=(0, 0, 10, 10)
            ),
            LayoutRegion(
                id="sized", surface_id="wrong-size", kind="text", bbox=(0, 0, 10, 10)
            ),
        ),
    )
    result = PdfminerTextReader().read(data, layout=layout)
    assert result.outcome == "partial"
    assert [(b.region_id, b.text) for b in result.blocks] == [
        ("text", "Shipping labels\nCost: 90 USD")
    ]
    assert [(f.code, f.region_id, f.page_index) for f in result.findings] == [
        ("selection_unsupported", "picture", 0),
        ("page_missing", "far", 8),
        ("layout_mismatch", "sized", 1),
    ]


# Passwords ---------------------------------------------------------------


def test_empty_user_password_opens_without_a_password(make_reader_pdf):
    data = make_reader_pdf((text_page("Open"),), password="")
    assert [b.text for b in PdfminerTextReader().read(data).blocks] == ["Open"]


def test_correct_password_opens(make_reader_pdf):
    data = make_reader_pdf((text_page("Secret"),), password="private-pass")
    result = PdfminerTextReader().read(Artifact(data=data, password="private-pass"))
    assert [b.text for b in result.blocks] == ["Secret"]
    assert "private-pass" not in result.model_dump_json()


@pytest.mark.parametrize(
    "given,code", [(None, "password_required"), ("wrong-private", "invalid_password")]
)
def test_missing_or_wrong_password_fails_safely(make_reader_pdf, given, code):
    data = make_reader_pdf((text_page("Secret"),), password="private-pass")
    result = PdfminerTextReader().read(Artifact(data=data, password=given))
    assert result.outcome == "failed" and not result.blocks
    assert [f.code for f in result.findings] == [code]
    dumped = result.model_dump_json()
    assert "private" not in dumped and "Secret" not in dumped


# Invalid sources -----------------------------------------------------------


def test_non_pdf_is_not_applicable():
    result = PdfminerTextReader().read(b"private plain text")
    assert result.outcome == "not_applicable"
    assert [f.code for f in result.findings] == ["not_applicable"]
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("data", [b"", b"%PDF-1.7\nprivate garbage"])
def test_empty_or_broken_pdf_fails_safely(data):
    result = PdfminerTextReader().read(data)
    assert result.outcome == "failed" and not result.blocks
    assert [f.code for f in result.findings] == ["decode_failed"]
    assert "private" not in result.model_dump_json()


def test_unreadable_page_keeps_other_pages(make_reader_pdf, monkeypatch):
    from pdfminer.pdfinterp import PDFPageInterpreter
    from pdfminer.pdftypes import PDFException

    real = PDFPageInterpreter.process_page
    calls = []

    def flaky(self, page):
        calls.append(1)
        if len(calls) == 2:
            raise PDFException("private backend detail")
        return real(self, page)

    monkeypatch.setattr(PDFPageInterpreter, "process_page", flaky)
    data = make_reader_pdf((text_page("One"), text_page("Two"), text_page("Three")))
    result = PdfminerTextReader().read(data)
    assert result.outcome == "partial"
    assert [(b.page_index, b.text) for b in result.blocks] == [(0, "One"), (2, "Three")]
    assert [(f.code, f.page_index) for f in result.findings] == [("page_unreadable", 1)]
    assert "private" not in result.model_dump_json()


def test_missing_and_nonregular_files_are_not_opened(tmp_path, monkeypatch):
    reader = PdfminerTextReader()
    result = reader.read(tmp_path / "private-name.pdf")
    assert [f.code for f in result.findings] == ["source_unreadable"]
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

    def without_pdfminer(name, *args, **kwargs):
        if name == "pdfminer" or name.startswith("pdfminer."):
            raise ImportError("no pdfminer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pdfminer)
    with pytest.raises(ImportError, match=r"raggae\[text-pdf\]"):
        PdfminerTextReader().read(data)


def test_empty_selection_reads_nothing_and_says_so(
    tmp_path, monkeypatch, column_layout
):
    monkeypatch.setattr(
        type(tmp_path), "stat", lambda *a, **k: pytest.fail("must not stat")
    )
    monkeypatch.setattr(
        type(tmp_path), "open", lambda *a, **k: pytest.fail("must not open")
    )
    result = PdfminerTextReader().read(
        tmp_path / "missing.pdf", layout=column_layout, region_ids=[]
    )
    assert result.outcome == "success" and not result.blocks
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("nothing_selected", None)
    ]


def test_layout_argument_validation(tmp_path, column_layout):
    reader = PdfminerTextReader()
    source = tmp_path / "missing.pdf"
    with pytest.raises(ValueError, match="require a layout"):
        reader.read(source, region_ids=[])
    with pytest.raises(ValueError, match="belong"):
        reader.read(source, layout=column_layout, region_ids=["unknown"])
    with pytest.raises(TypeError, match="LayoutAnalysisResult"):
        reader.read(source, layout="wrong")


def test_streams_are_not_silently_added_to_source_contract(make_reader_pdf):
    with pytest.raises(TypeError, match="source must"):
        PdfminerTextReader().read(BytesIO(make_reader_pdf()))
