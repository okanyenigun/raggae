"""PDF geometry and the drawn-picture walk shared by the PDF image extractors."""

import pypdfium2 as pdfium
import pytest

from raggae.documents.image_extractor.pdf.pdf_regions import drawn_pictures
from raggae.documents.pdf_geometry import displayed_box, pdf_box
from tests.documents.image_extractor.conftest import draw


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_displayed_box_undoes_pdf_box(rotation):
    crop, unit = (10.0, 20.0, 310.0, 420.0), 2.0
    bbox = (30.0, 50.0, 110.0, 90.0)
    assert displayed_box(pdf_box(bbox, crop, rotation, unit), crop, rotation, unit) == (
        pytest.approx(bbox)
    )


def test_pictures_inside_nested_forms_are_placed_on_the_page(pdf):
    # Worked out by hand: the unit square scaled by 10, moved by the inner
    # form's Matrix (+3) and the outer form's drawing (+10), scaled by the outer
    # form's Matrix (2, +5), then moved by the page (+100, +200).
    image = pdf.picture("rgb")
    inner = pdf.form({"/Im": image}, draw("Im", 0, 0, 10, 10))
    inner.Matrix = [1, 0, 0, 1, 3, 3]
    outer = pdf.form({"/Inner": inner}, b"q 1 0 0 1 10 10 cm /Inner Do Q")
    outer.Matrix = [2, 0, 0, 2, 5, 5]
    pdf.page({"/Outer": outer}, b"q 1 0 0 1 100 200 cm /Outer Do Q")
    document = pdfium.PdfDocument(pdf.save())
    try:
        page = document[0]
        assert [bounds for _, bounds in drawn_pictures(page)] == [
            pytest.approx((131, 231, 151, 251))
        ]
        page.close()
    finally:
        document.close()
