import pikepdf
import pytest
import zipfile


@pytest.fixture
def active_pdf_factory(tmp_path):
    """Create tiny PDFs with explicitly selected catalog/page metadata."""
    def make_pdf(name="active.pdf", *, customize=None, pages=2, encryption=None):
        path = tmp_path / name
        with pikepdf.Pdf.new() as document:
            for _ in range(pages):
                document.add_blank_page(page_size=(72, 72))
            if customize is not None:
                customize(document)
            document.save(path, encryption=encryption)
        return path

    return make_pdf


@pytest.fixture
def ooxml_factory(tmp_path):
    """Tiny real ZIPs with selected Office part names, not full Office documents."""
    def make_archive(name="document.docx", *, entries=()):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member, content in entries:
                archive.writestr(member, content)
        return path

    return make_archive
