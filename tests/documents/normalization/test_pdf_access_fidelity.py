"""Preserve rich PDF content and appearance when removing password protection.

These fixtures use existing dependencies and no system fonts or external assets.
The visual comparison additionally needs Poppler's pdftoppm; no tool is installed
by the tests. Equality covers these fixtures, not every possible PDF feature.
"""

from io import BytesIO
from pathlib import Path
import shutil
import subprocess

import pikepdf
from PIL import Image
from pypdf import PdfReader
import pytest

from raggae.documents.normalization import (
    Artifact,
    NormalizationRequest,
    PdfDecryptionNormalizer,
)


USER_PASSWORD = "fixture-user-password"
OWNER_PASSWORD = "fixture-owner-password"


def rich_encrypted_pdf() -> bytes:
    """Two pages, embedded figures/font, editable field, navigation and attachment."""
    with pikepdf.Pdf.new() as pdf, BytesIO() as output:
        first = pdf.add_blank_page(page_size=(420, 600))
        second = pdf.add_blank_page(page_size=(600, 420))
        for page in (first, second):
            page.obj.CropBox = pikepdf.Array([12, 12, *list(page.obj.MediaBox)[2:]])
            page.obj.TrimBox = pikepdf.Array([18, 18, *list(page.obj.MediaBox)[2:]])
            page.obj.BleedBox = page.obj.CropBox
            page.obj.UserUnit = 1
        second.obj.Rotate = 90

        regular_font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding,
        ))
        # Type 3 stores its glyph drawing programs inside the PDF: an embedded
        # font without a dependency on a font file on the developer's machine.
        glyph = pdf.make_stream(
            b"600 0 0 0 600 700 d1 "
            b"50 0 m 300 700 l 550 0 l 440 0 l 375 180 l 225 180 l 160 0 l h "
            b"260 250 m 340 250 l 300 430 l h f*"
        )
        unicode_map = pdf.make_stream(
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /FixtureUnicode def /CMapType 2 def\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"1 beginbfchar <41> <0041> endbfchar\n"
            b"endcmap CMapName currentdict /CMap defineresource pop end end"
        )
        embedded_font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3,
            Name=pikepdf.Name.EmbeddedA, FontBBox=pikepdf.Array([0, 0, 600, 700]),
            FontMatrix=pikepdf.Array([0.001, 0, 0, 0.001, 0, 0]),
            CharProcs=pikepdf.Dictionary(A=glyph),
            Encoding=pikepdf.Dictionary(Differences=pikepdf.Array([65, pikepdf.Name.A])),
            FirstChar=65, LastChar=65, Widths=pikepdf.Array([600]),
            Resources=pikepdf.Dictionary(), ToUnicode=unicode_map,
        ))

        with Image.new("RGB", (48, 32)) as image, BytesIO() as jpeg:
            image.putdata([
                (x * 5, y * 7, 180 if (x // 8 + y // 8) % 2 else 40)
                for y in range(32) for x in range(48)
            ])
            raw_figure = pdf.make_stream(image.tobytes())
            image.save(jpeg, format="JPEG", quality=85)
            jpeg_figure = pdf.make_stream(jpeg.getvalue())
        for figure in (raw_figure, jpeg_figure):
            figure.Type = pikepdf.Name.XObject
            figure.Subtype = pikepdf.Name.Image
            figure.Width, figure.Height = 48, 32
            figure.ColorSpace = pikepdf.Name.DeviceRGB
            figure.BitsPerComponent = 8
        jpeg_figure.Filter = pikepdf.Name.DCTDecode
        mask = pdf.make_stream(bytes(255 if x < 24 else 100 for y in range(32) for x in range(48)))
        mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        mask.Width, mask.Height = 48, 32
        mask.ColorSpace, mask.BitsPerComponent = pikepdf.Name.DeviceGray, 8
        raw_figure.SMask = mask

        resources = pdf.make_indirect(pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=regular_font, F2=embedded_font),
            XObject=pikepdf.Dictionary(RawFigure=raw_figure, JpegFigure=jpeg_figure),
        ))
        for page in (first, second):
            page.obj.Resources = resources
        first.obj.Contents = pdf.make_stream(
            b"BT /F1 22 Tf 36 555 Td (PDF preservation fixture) Tj ET\n"
            b"BT /F1 12 Tf 36 525 Td (Text, figures, an embedded font and a form.) Tj ET\n"
            b"q 0.1 0.4 0.7 RG 2 w 36 355 340 145 re S "
            b"36 390 m 376 390 l S 206 355 m 206 500 l S Q\n"
            b"q 120 0 0 80 50 405 cm /RawFigure Do Q\n"
            b"q 120 0 0 80 225 405 cm /JpegFigure Do Q\n"
            b"BT /F2 32 Tf 50 365 Td (A) Tj ET\n"
            b"BT /F1 12 Tf 36 338 Td (Customer name:) Tj ET\n"
            b"BT /F1 12 Tf 36 270 Td (Link to page two) Tj ET\n"
            b"BT /F1 10 Tf 36 35 Td (Page 1 - portrait) Tj ET"
        )
        second.obj.Contents = pdf.make_stream(
            b"BT /F1 22 Tf 36 370 Td (Second page) Tj ET\n"
            b"BT /F1 12 Tf 36 340 Td (This page retains its rotation and crop box.) Tj ET\n"
            b"q 0.8 0.2 0.1 rg 36 190 220 110 re f Q\n"
            b"q 120 0 0 80 300 190 cm /JpegFigure Do Q\n"
            b"BT /F2 42 Tf 400 150 Td (A) Tj ET\n"
            b"BT /F1 12 Tf 36 145 Td (Same editable customer field:) Tj ET\n"
            b"BT /F1 10 Tf 36 35 Td (Page 2 - rotated landscape) Tj ET"
        )

        note = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
            Rect=pikepdf.Array([365, 515, 390, 540]), Contents="Keep this note",
            C=pikepdf.Array([1, 0.8, 0]), F=4, P=first.obj,
        ))
        link = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
            Rect=pikepdf.Array([36, 265, 160, 284]), Border=pikepdf.Array([0, 0, 1]),
            A=pikepdf.Dictionary(S=pikepdf.Name.GoTo, D="second-page"),
        ))
        first.obj.Annots = pikepdf.Array([note, link])
        second.obj.Annots = pikepdf.Array()

        field = pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Tx, T="customer", V="Jane", DV="Jane", Ff=0,
            DA="/F1 12 Tf 0 g", Kids=pikepdf.Array(),
        ))
        appearance = pdf.make_stream(
            b"q 1 g 0 0 194 28 re f 0.3 G 1 w 0.5 0.5 193 27 re S "
            b"BT /F1 12 Tf 0 g 6 9 Td (Jane) Tj ET Q"
        )
        appearance.Type, appearance.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        appearance.BBox = pikepdf.Array([0, 0, 194, 28])
        appearance.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=regular_font))
        for page, bottom in ((first, 300), (second, 105)):
            widget = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
                Rect=pikepdf.Array([36, bottom, 230, bottom + 28]),
                Parent=field, P=page.obj, F=4, AP=pikepdf.Dictionary(N=appearance),
            ))
            field.Kids.append(widget)
            page.obj.Annots.append(widget)
        pdf.Root.AcroForm = pikepdf.Dictionary(
            Fields=pikepdf.Array([field]), NeedAppearances=False,
            DR=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=regular_font)),
        )

        attachment = pdf.make_stream(b"Keep the embedded attachment, too.\n")
        attachment.Type = pikepdf.Name.EmbeddedFile
        file_spec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F="notes.txt", UF="notes.txt",
            EF=pikepdf.Dictionary(F=attachment), Desc="Fixture attachment",
        ))
        pdf.Root.Names = pikepdf.Dictionary(
            EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array(["notes.txt", file_spec])),
            Dests=pikepdf.Dictionary(Names=pikepdf.Array([
                "second-page", pikepdf.Dictionary(D=pikepdf.Array([second.obj, pikepdf.Name.Fit])),
            ])),
        )
        outlines = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Outlines, Count=1))
        bookmark = pdf.make_indirect(pikepdf.Dictionary(
            Title="Second page", Parent=outlines,
            Dest=pikepdf.Array([second.obj, pikepdf.Name.Fit]),
        ))
        outlines.First, outlines.Last = bookmark, bookmark
        pdf.Root.Outlines = outlines
        pdf.docinfo["/Title"], pdf.docinfo["/Author"] = "Fidelity fixture", "Jane"
        pdf.save(output, encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD))
        return output.getvalue()


@pytest.fixture(scope="module")
def encrypted_fixture():
    return rich_encrypted_pdf()


def content_snapshot(data: bytes, password: str = "") -> dict:
    """Compare content, not object numbers, compression or encrypted file bytes."""
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        assert reader.decrypt(password)
    text = tuple(page.extract_text() for page in reader.pages)
    assert "PDF preservation fixture" in text[0]
    assert "Second page" in text[1]
    assert reader.get_fields()["customer"]["/V"] == "Jane"
    assert reader.get_destination_page_number(reader.named_destinations["second-page"]) == 1

    with pikepdf.Pdf.open(BytesIO(data), password=password) as pdf:
        pages = []
        for page in pdf.pages:
            font = page.obj.Resources.Font.F2
            assert str(font.Subtype) == "/Type3"
            raw = page.obj.Resources.XObject.RawFigure
            jpeg = page.obj.Resources.XObject.JpegFigure
            assert raw.read_bytes() and jpeg.read_raw_bytes() and raw.SMask.read_bytes()
            annotations = []
            for annotation in page.obj.Annots:
                item = {"subtype": str(annotation.Subtype), "rect": tuple(annotation.Rect)}
                if annotation.Subtype == pikepdf.Name.Text:
                    item["contents"] = str(annotation.Contents)
                elif annotation.Subtype == pikepdf.Name.Link:
                    item["action"] = (str(annotation.A.S), str(annotation.A.D))
                else:
                    assert annotation.Subtype == pikepdf.Name.Widget
                    assert annotation.Parent.objgen == pdf.Root.AcroForm.Fields[0].objgen
                    item["field"] = (str(annotation.Parent.T), str(annotation.Parent.V))
                    item["appearance"] = annotation.AP.N.read_bytes()
                    assert item["appearance"]
                annotations.append(item)
            pages.append({
                "geometry": {key: tuple(page.obj[key]) for key in
                             ("/MediaBox", "/CropBox", "/TrimBox", "/BleedBox")},
                "rotation": int(page.obj.get("/Rotate", 0)),
                "user_unit": int(page.obj.UserUnit),
                "contents": page.obj.Contents.read_bytes(),
                "regular_font": str(page.obj.Resources.Font.F1.BaseFont),
                "embedded_font": (font.CharProcs.A.read_bytes(), font.ToUnicode.read_bytes(),
                                  tuple(font.Widths), tuple(font.FontBBox), tuple(font.FontMatrix),
                                  tuple(str(item) for item in font.Encoding.Differences)),
                "images": (raw.read_bytes(), raw.SMask.read_bytes(), jpeg.read_raw_bytes(),
                           int(raw.Width), int(raw.Height), str(raw.ColorSpace),
                           int(raw.BitsPerComponent), int(jpeg.Width), int(jpeg.Height),
                           str(jpeg.ColorSpace), str(jpeg.Filter), int(jpeg.BitsPerComponent)),
                "annotations": annotations,
            })
        field = pdf.Root.AcroForm.Fields[0]
        assert len(field.Kids) == 2
        for page, widget in zip(pdf.pages, field.Kids, strict=True):
            assert widget.P.objgen == page.obj.objgen
            assert any(item.objgen == widget.objgen for item in page.obj.Annots)
        assert pdf.Root.Outlines.First.Dest[0].objgen == pdf.pages[1].obj.objgen
        assert pdf.Root.Names.Dests.Names[1].D[0].objgen == pdf.pages[1].obj.objgen
        return {
            "text": text, "pages": pages,
            "form": (str(field.T), str(field.V), str(field.DV), str(field.FT),
                     int(field.Ff), str(field.DA), bool(pdf.Root.AcroForm.NeedAppearances)),
            "metadata": {str(key): str(value) for key, value in pdf.docinfo.items()},
            "attachment": (str(pdf.Root.Names.EmbeddedFiles.Names[0]),
                           str(pdf.Root.Names.EmbeddedFiles.Names[1].F),
                           pdf.Root.Names.EmbeddedFiles.Names[1].EF.F.read_bytes()),
            "bookmark": str(pdf.Root.Outlines.First.Title),
        }


@pytest.mark.parametrize("password", [USER_PASSWORD, OWNER_PASSWORD])
@pytest.mark.parametrize("source_kind", ["path", "bytes", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_unlock_preserves_rich_document_content(encrypted_fixture, password, source_kind, disk, tmp_path):
    source = Artifact(data=encrypted_fixture, password=password)
    if source_kind == "path":
        path = tmp_path / "original.pdf"
        path.write_bytes(encrypted_fixture)
        source = Artifact(path=path, password=password)
    elif source_kind == "artifact":
        source = Artifact(data=encrypted_fixture, password=password)
    request = NormalizationRequest(
        source=source, output_path=tmp_path / "unlocked.pdf" if disk else None,
    )
    before = content_snapshot(encrypted_fixture, password)
    assert len(before["pages"]) == 2
    assert before["pages"][0]["rotation"] == 0
    assert before["pages"][1]["rotation"] == 90
    assert before["pages"][0]["geometry"]["/MediaBox"] == (0, 0, 420, 600)
    assert before["pages"][1]["geometry"]["/MediaBox"] == (0, 0, 600, 420)
    assert [item["subtype"] for item in before["pages"][0]["annotations"]] == ["/Text", "/Link", "/Widget"]
    assert before["attachment"][2] == b"Keep the embedded attachment, too.\n"
    result = PdfDecryptionNormalizer().normalize(request)
    assert result.outcome == "changed"
    assert result.report.page_count == 2
    unlocked = result.artifact.read_bytes()
    with pikepdf.Pdf.open(BytesIO(unlocked)) as pdf:
        assert not pdf.is_encrypted
    assert content_snapshot(unlocked) == before
    assert request.source.read_bytes() == encrypted_fixture
    assert not list(tmp_path.glob(".raggae-*.part"))


def render_pages(path: Path, prefix: Path, password: str = "") -> list[Path]:
    executable = shutil.which("pdftoppm")
    if executable is None:
        pytest.skip("Visual fidelity comparison requires Poppler's pdftoppm")
    command = [executable, "-r", "96", "-png"]
    if password:
        command.extend(["-upw", password])
    subprocess.run(command + [str(path), str(prefix)], check=True,
                   capture_output=True, timeout=30)
    return sorted(prefix.parent.glob(prefix.name + "-*.png"))


@pytest.mark.parametrize("password", [USER_PASSWORD, OWNER_PASSWORD])
def test_unlock_preserves_every_rendered_pixel(encrypted_fixture, password, tmp_path):
    original = tmp_path / "original.pdf"
    original.write_bytes(encrypted_fixture)
    result = PdfDecryptionNormalizer().normalize(NormalizationRequest(
        source=Artifact(path=original, password=password), output_path=tmp_path / "unlocked.pdf",
    ))
    assert result.outcome == "changed"
    before = render_pages(original, tmp_path / "before", USER_PASSWORD)
    after = render_pages(result.artifact.path, tmp_path / "after")
    assert len(before) == len(after) == 2
    for original_page, unlocked_page in zip(before, after, strict=True):
        with Image.open(original_page) as first, Image.open(unlocked_page) as second:
            assert first.size == second.size
            assert first.mode == second.mode
            assert first.tobytes() == second.tobytes()
