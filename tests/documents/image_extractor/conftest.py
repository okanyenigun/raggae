"""Self-contained PDF fixtures for image extraction; no downloaded files."""

import zlib
from io import BytesIO

import pikepdf
import pytest
from PIL import Image


def encoded(mode, size, color, format):
    with BytesIO() as output:
        Image.new(mode, size, color).save(output, format)
        return output.getvalue()


class PdfBuilder:
    """Build a PDF picture by picture; objects can be shared between pages."""

    def __init__(self):
        self.document = pikepdf.Pdf.new()

    def picture(self, kind="rgb", **extra):
        name = pikepdf.Name
        width = height = 2
        options = {"BitsPerComponent": 8, "ColorSpace": name.DeviceRGB}
        if kind == "jpeg":
            data = encoded("RGB", (4, 3), (0, 128, 255), "JPEG")
            width, height = 4, 3
            options["Filter"] = name.DCTDecode
        elif kind == "rgb":
            data = zlib.compress(bytes([200, 30, 30]) * 4)
            options["Filter"] = name.FlateDecode
        elif kind == "cmyk":
            data = bytes([0, 100, 200, 10]) * 4
            options["ColorSpace"] = name.DeviceCMYK
        elif kind == "jpx":
            data = encoded("RGB", (5, 4), (0, 128, 255), "JPEG2000")
            width, height = 5, 4
            options = {"Filter": name.JPXDecode}
        elif kind == "masked":
            data = bytes([50, 60, 70]) * 4
            options["SMask"] = self.picture(
                "raw", data=bytes([0, 255, 128, 64]), ColorSpace=name.DeviceGray
            )
        elif kind == "separation":
            tint = self.document.make_indirect(
                pikepdf.Dictionary(
                    FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[1, 0, 0, 0], N=1
                )
            )
            data = bytes([0, 128, 255, 64])
            options["ColorSpace"] = pikepdf.Array(
                [name.Separation, name("/Spot"), name.DeviceCMYK, tint]
            )
        elif kind == "corrupt":
            data = b"this is not zlib data"
            options["Filter"] = name.FlateDecode
        elif kind == "raw":
            data = extra.pop("data")
        else:
            raise ValueError(kind)
        stream = self.document.make_stream(data)
        stream.Type = name.XObject
        stream.Subtype = name.Image
        stream.Width = extra.pop("Width", width)
        stream.Height = extra.pop("Height", height)
        for key, value in {**options, **extra}.items():
            stream[name("/" + key)] = value
        return stream

    def form(self, xobjects=None, content=b""):
        stream = self.document.make_stream(content)
        stream.Type = pikepdf.Name.XObject
        stream.Subtype = pikepdf.Name.Form
        stream.BBox = [0, 0, 10, 10]
        stream.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(xobjects or {})
        )
        return stream

    def page(self, xobjects=None, content=b"", *, rotate=0, crop=None):
        page = self.document.add_blank_page(page_size=(300, 400))
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(xobjects or {}))
        page.Contents = self.document.make_stream(content)
        page.Rotate = rotate
        if crop is not None:
            page.CropBox = pikepdf.Array(crop)
        return page

    def save(self, password=None):
        options = {}
        if password is not None:
            options["encryption"] = pikepdf.Encryption(
                user=password, owner="extractor-fixture-owner"
            )
        with BytesIO() as output:
            self.document.save(output, **options)
            return output.getvalue()


def picture_file(color="red", format="PNG", size=(40, 30)):
    """An image file in memory, ready for python-docx, python-pptx, or openpyxl."""
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format)
    output.seek(0)
    return output


# A minimal placeable WMF header, enough for Pillow to identify a WMF file.
WMF = (
    b"\xd7\xcd\xc6\x9a\x00\x00"
    + (0).to_bytes(2, "little", signed=True) * 2
    + (100).to_bytes(2, "little", signed=True) * 2
    + (1440).to_bytes(2, "little")
    + bytes(6)
    + b"\x01\x00\x09\x00"
    + bytes(64)
)


def rezip(data, replace=None, add=None):
    """Rewrite an Office package: ``replace`` maps part names to new bytes or to a
    function of the old bytes; ``add`` adds new parts."""
    from zipfile import ZIP_DEFLATED, ZipFile

    replace, add = replace or {}, add or {}
    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            change = replace.get(item.filename)
            if change is not None:
                content = change(content) if callable(change) else change
            target.writestr(item, content)
        for name, content in add.items():
            target.writestr(name, content)
    return output.getvalue()


def make_layout(*regions, pages=1, size=(300, 400)):
    """A PDF layout with ``pages`` page surfaces of ``size`` (displayed points).

    Each region is (id, page_number, kind, bbox), with bbox measured from the
    displayed page's top-left corner.
    """
    from raggae.documents.layout_analysis import (
        LayoutAnalysisResult,
        LayoutRegion,
        LayoutSurface,
        PdfPageLocation,
    )

    return LayoutAnalysisResult(
        outcome="success",
        surfaces=tuple(
            LayoutSurface(
                id_=f"page-{number}",
                location=PdfPageLocation(page_number=number),
                width=size[0],
                height=size[1],
            )
            for number in range(1, pages + 1)
        ),
        regions=tuple(
            LayoutRegion(id=identifier, surface_id=f"page-{page}", kind=kind, bbox=bbox)
            for identifier, page, kind, bbox in regions
        ),
    )


def draw(name, left, bottom, width, height):
    """Drawing commands that paint a picture at a PDF-space rectangle."""
    return f"q {width} 0 0 {height} {left} {bottom} cm /{name} Do Q\n".encode()


INLINE = b"q 40 0 0 40 200 60 cm BI /W 4 /H 4 /BPC 8 /CS /G ID " + bytes(16) + b" EI Q\n"


@pytest.fixture
def pdf():
    builder = PdfBuilder()
    yield builder
    builder.document.close()


@pytest.fixture
def jpeg_pdf(pdf):
    """One page with one stored JPEG; returns (pdf bytes, stored JPEG bytes)."""
    picture = pdf.picture("jpeg")
    pdf.page({"/Photo": picture}, b"q 40 0 0 30 10 10 cm /Photo Do Q")
    return pdf.save(), picture.read_raw_bytes()
