import pikepdf
import pytest


@pytest.fixture
def capability_pdf_factory(tmp_path):
    """Tiny real PDFs with ASCII text, without rendering or external programs."""
    def make_pdf(name="text.pdf", *, texts=("A" * 25,), encryption=None):
        path = tmp_path / name
        with pikepdf.Pdf.new() as document:
            font = document.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica))
            for text in texts:
                page = document.add_blank_page(page_size=(200, 200))
                page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
                escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands = f"BT /F1 10 Tf 10 100 Td ({escaped}) Tj ET" if text else ""
                page.obj.Contents = document.make_stream(commands.encode("ascii"))
            document.save(path, encryption=encryption)
        return path

    return make_pdf


@pytest.fixture
def scanned_pdf_factory(tmp_path):
    """One RGB pixel drawn directly or through forms; no OCR or rendering."""
    def make_pdf(*, nesting=0, indirect_resources=False, indirect_xobjects=False):
        path = tmp_path / "scan.pdf"
        with pikepdf.Pdf.new() as document:
            page = document.add_blank_page(page_size=(72, 72))
            image = document.make_stream(bytes([30, 60, 90]))
            image.Type = pikepdf.Name.XObject
            image.Subtype = pikepdf.Name.Image
            image.Width = 1
            image.Height = 1
            image.ColorSpace = pikepdf.Name.DeviceRGB
            image.BitsPerComponent = 8
            objects = pikepdf.Dictionary(Im=image)
            commands = "q 20 0 0 20 0 0 cm /Im Do Q"

            def make_resources(xobjects):
                if indirect_xobjects:
                    xobjects = document.make_indirect(xobjects)
                resources = pikepdf.Dictionary(XObject=xobjects)
                return document.make_indirect(resources) if indirect_resources else resources

            for _ in range(nesting):
                form = document.make_stream(commands.encode("ascii"))
                form.Type = pikepdf.Name.XObject
                form.Subtype = pikepdf.Name.Form
                form.BBox = pikepdf.Array([0, 0, 72, 72])
                form.Resources = make_resources(objects)
                objects = pikepdf.Dictionary(Fm=form)
                commands = "/Fm Do"
            page.obj.Resources = make_resources(objects)
            page.obj.Contents = document.make_stream(commands.encode("ascii"))
            document.save(path)
        return path

    return make_pdf
