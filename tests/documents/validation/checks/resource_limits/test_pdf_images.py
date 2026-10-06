from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.validation import PdfLimits, PdfResourceProbe
from raggae.documents.validation.checks.resource_limits import pdf as pdf_module
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code


@pytest.fixture
def image_pdf_factory(tmp_path):
    """Real, tiny RGB images, optionally inside nested form XObjects."""
    def make_pdf(*, page_images, page_sizes=None, nested=False):
        path = tmp_path / "images.pdf"
        with pikepdf.Pdf.new() as document:
            for number, dimensions in enumerate(page_images):
                size = page_sizes[number] if page_sizes is not None else (72, 72)
                page = document.add_blank_page(page_size=size)
                resources = pikepdf.Dictionary()
                for index, (width, height) in enumerate(dimensions):
                    stream = document.make_stream(bytes([30, 60, 90]) * width * height)
                    stream.Type = pikepdf.Name.XObject
                    stream.Subtype = pikepdf.Name.Image
                    stream.Width = width
                    stream.Height = height
                    stream.ColorSpace = pikepdf.Name.DeviceRGB
                    stream.BitsPerComponent = 8
                    resources[f"/Im{index}"] = stream
                commands = "\n".join(f"q 10 0 0 10 0 0 cm /Im{i} Do Q" for i in range(len(dimensions)))
                if nested:
                    # Two form layers exercise recursive metadata discovery.
                    for _ in range(2):
                        form = document.make_stream(commands.encode("ascii"))
                        form.Type = pikepdf.Name.XObject
                        form.Subtype = pikepdf.Name.Form
                        form.BBox = pikepdf.Array([0, 0, 72, 72])
                        form.Resources = pikepdf.Dictionary(XObject=resources)
                        resources = pikepdf.Dictionary(Fm=form)
                        commands = "/Fm Do"
                page.obj.Resources = pikepdf.Dictionary(XObject=resources)
                page.obj.Contents = document.make_stream(commands.encode("ascii"))
            document.save(path)
        return path

    return make_pdf


@pytest.mark.parametrize("size", [(1, 2), (1, 3), (2, 2)])
@pytest.mark.parametrize("nested", [False, True])
def test_real_image_pixel_boundary_without_decoding(image_pdf_factory, monkeypatch, forbidden_operation, size, nested):
    path = image_pdf_factory(page_images=[[size]], nested=nested)
    before = path.read_bytes()
    # Independently establish that the fixture contains an image with these dimensions.
    with pikepdf.Pdf.open(path) as document:
        images = tuple(document.pages[0].get_images().values())
        assert len(images) == 1
        assert (int(images[0].Width), int(images[0].Height)) == size
    monkeypatch.setattr(pikepdf.PdfImage, "as_pil_image", forbidden_operation)
    monkeypatch.setattr(pikepdf.PdfImage, "extract_to", forbidden_operation)
    outcome = PdfResourceProbe(PdfLimits(max_image_pixels=3)).check(path)
    pixels = size[0] * size[1]
    assert outcome.codes == ((Code.IMAGE_TOO_LARGE,) if pixels > 3 else ())
    if pixels > 3:
        assert dict(outcome.findings[0].detail) == {"limit": 3, "observed": pixels, "page": 1}
    assert dict(outcome.facts) == {"page_count": 1}
    assert path.read_bytes() == before


@pytest.mark.parametrize("nested", [False, True])
def test_real_multiple_images_and_pages_report_first_excess_once(image_pdf_factory, nested):
    path = image_pdf_factory(page_images=[[(1, 1)], [(1, 2), (2, 2)], [(3, 3)]], nested=nested)
    outcome = PdfResourceProbe(PdfLimits(max_image_pixels=3)).check(path)
    assert outcome.codes == (Code.IMAGE_TOO_LARGE,)
    assert dict(outcome.findings[0].detail) == {"limit": 3, "observed": 4, "page": 2}
    assert outcome.facts["page_count"] == 3


@pytest.mark.parametrize("same_page", [False, True])
def test_real_combined_page_and_image_limits(image_pdf_factory, same_page):
    if same_page:
        path = image_pdf_factory(page_images=[[(2, 2)], [(3, 3)]], page_sizes=[(101, 72), (102, 72)])
        image_page = 1
    else:
        path = image_pdf_factory(page_images=[[], [(2, 2)], [(3, 3)]], page_sizes=[(101, 72), (102, 72), (103, 72)])
        image_page = 2
    outcome = PdfResourceProbe(PdfLimits(max_page_points=100, max_image_pixels=3)).check(path)
    assert outcome.codes == (Code.PAGE_TOO_LARGE, Code.IMAGE_TOO_LARGE)
    assert outcome.findings[0].detail["page"] == 1
    assert outcome.findings[1].detail["page"] == image_page


def test_page_scanning_stops_after_both_categories(monkeypatch, missing_path):
    class Pages:
        def __len__(self):
            return 2

        def __iter__(self):
            yield SimpleNamespace(mediabox=(0, 0, 101, 72),
                                  get_images=lambda: {"/Im": SimpleNamespace(Width=2, Height=2)})
            pytest.fail("No more pages should be inspected after both limits were reported")

    class Document:
        pages = Pages()
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfResourceProbe(PdfLimits(max_page_points=100, max_image_pixels=3)).check(missing_path)
    assert outcome.codes == (Code.PAGE_TOO_LARGE, Code.IMAGE_TOO_LARGE)
    assert document.closed


@pytest.mark.parametrize("image", [SimpleNamespace(), SimpleNamespace(Width=0, Height=2),
                                 SimpleNamespace(Width=2, Height=0), SimpleNamespace(Width=None, Height=2)])
def test_missing_and_zero_image_dimensions_do_not_invent_pixels(monkeypatch, missing_path, image):
    class Document:
        pages = [SimpleNamespace(mediabox=(0, 0, 72, 72), get_images=lambda: {"/Im": image})]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: Document()))
    assert PdfResourceProbe(PdfLimits(max_image_pixels=1)).check(missing_path).codes == ()
