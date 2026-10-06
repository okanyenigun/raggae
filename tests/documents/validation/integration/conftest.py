import zipfile

import pikepdf
from PIL import Image
import pytest


@pytest.fixture
def integration_image(tmp_path):
    def make_image(extension="png", format_name="PNG"):
        path = tmp_path / f"image.{extension}"
        with Image.new("RGB", (3, 2), (30, 60, 90)) as image:
            image.save(path, format=format_name)
        return path
    return make_image


@pytest.fixture
def integration_office(tmp_path):
    """Minimal metadata-focused OOXML containers, not full Office-app fixtures."""
    def make_office(format_name="docx", *, extra_parts=None):
        path = tmp_path / f"document.{format_name}"
        body = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[format_name]
        parts = {
            body: "<document/>",
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            "_rels/.rels": "<Relationships/>",
        }
        parts.update(extra_parts or {})
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return path
    return make_office


@pytest.fixture
def integration_pdf(tmp_path):
    """Tiny PDFs built using the same unrendered object model as worker fixtures."""
    def make_pdf(*, kind="text", pages=1, encryption=None, launch=False):
        path = tmp_path / "document.pdf"
        with pikepdf.Pdf.new() as document:
            font = document.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica,
            ))
            for _ in range(pages):
                page = document.add_blank_page(page_size=(200, 200))
                if kind == "text":
                    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
                    page.obj.Contents = document.make_stream(b"BT /F1 10 Tf 10 100 Td (This is sufficiently long document text.) Tj ET")
                elif kind == "scan":
                    image = document.make_stream(bytes([30, 60, 90]))
                    image.Type = pikepdf.Name.XObject
                    image.Subtype = pikepdf.Name.Image
                    image.Width, image.Height = 1, 1
                    image.ColorSpace = pikepdf.Name.DeviceRGB
                    image.BitsPerComponent = 8
                    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im=image))
                    page.obj.Contents = document.make_stream(b"q 20 0 0 20 0 0 cm /Im Do Q")
                elif kind != "blank":
                    raise ValueError("Unsupported test PDF kind")
            if launch:
                document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.Launch, F="never-run.exe")
            document.save(path, encryption=encryption)
        return path
    return make_pdf
