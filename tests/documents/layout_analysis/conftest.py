"""Small, generated PDF fixtures: no downloaded documents or extra fixture libraries."""

from io import BytesIO

import pikepdf
import pytest


@pytest.fixture
def make_pdf():
    def create(
        contents=(b"",), *, image=False, rotation=0, crop=None, media=None,
        unit=1, password=None, annotations=False,
    ):
        with pikepdf.Pdf.new() as document, BytesIO() as output:
            for content in contents:
                page = document.add_blank_page(page_size=(300, 400))
                font = document.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                ))
                page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
                if image:
                    picture = document.make_stream(bytes([200, 50, 20]) * 4)
                    picture.Type = pikepdf.Name.XObject
                    picture.Subtype = pikepdf.Name.Image
                    picture.Width = picture.Height = 2
                    picture.BitsPerComponent = 8
                    picture.ColorSpace = pikepdf.Name.DeviceRGB
                    page.Resources.XObject = pikepdf.Dictionary(Im1=picture)
                page.Contents = document.make_stream(content)
                page.Rotate = rotation
                page.UserUnit = unit
                if crop is not None:
                    page.CropBox = pikepdf.Array(crop)
                if media is not None:
                    page.MediaBox = pikepdf.Array(media)
                if annotations:
                    page.Annots = pikepdf.Array([document.make_indirect(pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
                        Rect=pikepdf.Array([10, 10, 30, 30]),
                        Contents="A fixture annotation",
                    ))])
            options = {}
            if password is not None:
                options["encryption"] = pikepdf.Encryption(
                    user=password, owner="layout-fixture-owner",  # ggignore: synthetic fixture password.
                )
            document.save(output, **options)
            return output.getvalue()
    return create


@pytest.fixture
def invoice_pdf(make_pdf):
    return make_pdf((b"""
BT /F1 14 Tf 30 380 Td (Report overview) Tj ET
BT /F1 12 Tf 30 330 Td (Left one) Tj 0 -14 Td (Left two) Tj ET
BT /F1 12 Tf 180 330 Td (Right one) Tj 0 -14 Td (Right two) Tj ET
40 200 m 240 200 l S 40 240 m 240 240 l S 40 280 m 240 280 l S
40 200 m 40 280 l S 140 200 m 140 280 l S 240 200 m 240 280 l S
BT /F1 12 Tf 50 260 Td (A) Tj 100 0 Td (B) Tj ET
BT /F1 12 Tf 50 220 Td (C) Tj 100 0 Td (D) Tj ET
q 20 0 0 20 250 350 cm /Im1 Do Q
""",), image=True)
