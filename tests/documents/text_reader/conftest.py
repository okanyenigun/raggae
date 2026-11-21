"""Self-contained PDF fixtures for native text reading; no downloaded files."""

from io import BytesIO

import pikepdf
import pytest


@pytest.fixture
def make_reader_pdf():
    def create(
        contents=(b"BT /F1 12 Tf 30 330 Td (Hello reader) Tj ET",),
        *,
        rotation=0,
        crop=None,
        media=None,
        unit=1,
        password=None,
        image=False,
    ):
        with pikepdf.Pdf.new() as document, BytesIO() as output:
            for content in contents:
                page = document.add_blank_page(page_size=(300, 400))
                font = document.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Font,
                        Subtype=pikepdf.Name.Type1,
                        BaseFont=pikepdf.Name.Helvetica,
                    )
                )
                page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
                if image:
                    picture = document.make_stream(bytes([160, 180, 200]) * 4)
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
            options = {}
            if password is not None:
                options["encryption"] = pikepdf.Encryption(
                    user=password,
                    owner="reader-fixture-owner",
                )
            document.save(output, **options)
            return output.getvalue()

    return create


@pytest.fixture
def columns_pdf(make_reader_pdf):
    return make_reader_pdf(
        (
            b"""
BT /F1 14 Tf 30 380 Td (Purchase report) Tj ET
BT /F1 12 Tf 30 330 Td (Left one) Tj 0 -14 Td (Left two) Tj ET
BT /F1 12 Tf 180 330 Td (Right one) Tj 0 -14 Td (Right two) Tj ET
""",
            b"BT /F1 12 Tf 30 330 Td (Follow-up note) Tj ET",
        )
    )
