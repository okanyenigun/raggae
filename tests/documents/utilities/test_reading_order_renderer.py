"""Returned links, honest sequence labels, source geometry and image cleanup."""

from io import BytesIO
import subprocess
import sys

from PIL import Image
import pytest

from raggae.documents.layout_analysis import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
    ReadingOrderEdge,
    ReadingOrderResult,
)
from raggae.documents.normalization import Artifact
from raggae.documents.utilities import (
    DocumentReadingOrderRenderer,
    DocumentRegionRenderer,
)
from .test_region_renderer import close, pdf_layout


def image_source(*, frames=1):
    with BytesIO() as data:
        images = [Image.new("RGB", (100, 120), "white") for _ in range(frames)]
        try:
            images[0].save(data, format="TIFF", save_all=True, append_images=images[1:])
            return data.getvalue()
        finally:
            for image in images:
                image.close()


def layout(*, numbers=(1,), pdf=False, width=100, height=120):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=tuple(
            LayoutSurface(
                id_=f"p{n}",
                kind="page" if pdf else "image",
                location=PdfPageLocation(page_number=n)
                if pdf
                else ImageFrameLocation(frame_number=n),
                width=width,
                height=height,
            )
            for n in numbers
        ),
        regions=tuple(
            LayoutRegion(id=f"p{n}-{name}", surface_id=f"p{n}", kind="text", bbox=box)
            for n in numbers
            for name, box in (("a", (20, 20, 40, 40)), ("b", (20, 80, 40, 100)))
        ),
    )


def order(pairs=(("p1-a", "p1-b"),), *, unresolved=()):
    return ReadingOrderResult(
        outcome="partial" if unresolved else "success",
        edges=tuple(
            ReadingOrderEdge(
                before_region_id=a, after_region_id=b, basis="vertical_gap"
            )
            for a, b in pairs
        ),
        unresolved_region_ids=unresolved,
        findings=(
            LayoutFinding(code="example.unresolved", message="Example ambiguity."),
        )
        if unresolved
        else (),
    )


def test_draws_only_returned_links_and_preserves_source_and_results(tmp_path):
    data = image_source()
    path = tmp_path / "source.tiff"
    path.write_bytes(data)
    mapped, sequence = layout(), order()
    snapshot = mapped.model_dump_json(), sequence.model_dump_json()
    renderer = DocumentReadingOrderRenderer(show_labels=False)
    images = renderer.render(path, mapped, sequence)
    try:
        assert images[1].mode == "RGB" and images[1].size == (100, 120)
        assert images[1].getpixel((30, 60)) == (148, 163, 184)
        assert images[1].getpixel((30, 90)) == (100, 116, 139)
        assert images[1].getpixel((20, 30)) == (37, 99, 235)
        images[1].save(tmp_path / "overlay.png")
        assert path.read_bytes() == data
        assert snapshot == (mapped.model_dump_json(), sequence.model_dump_json())
    finally:
        close(images)
    without_links = renderer.render(data, mapped, order(()))
    try:
        assert without_links[1].getpixel((30, 60)) == (255, 255, 255)
    finally:
        close(without_links)


@pytest.mark.parametrize(
    "ids,pairs,unresolved,expected",
    [
        (("b", "a", "c"), (("a", "b"), ("b", "c")), (), {"a": 1, "b": 2, "c": 3}),
        (("a", "b", "c"), (("a", "b"), ("a", "c")), (), {}),
        (("a", "b", "c"), (("a", "c"), ("b", "c")), (), {}),
        (("a", "b", "c"), (("a", "b"),), (), {}),
        (("a", "b"), (), (), {}),
        (("a", "b"), (("a", "b"),), ("b",), {}),
        (("a",), (), (), {"a": 1}),
        ((), (), (), {}),
    ],
)
def test_numbers_only_a_complete_unique_local_order(ids, pairs, unresolved, expected):
    edges = order(pairs).edges
    assert DocumentReadingOrderRenderer._ranks(ids, edges, set(unresolved)) == expected


def test_marks_unresolved_regions_red_and_with_a_question_mark(monkeypatch):
    from PIL import ImageDraw

    texts = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    images = DocumentReadingOrderRenderer().render(
        image_source(), layout(), order((), unresolved=("p1-b",))
    )
    try:
        assert images[1].getpixel((20, 90)) == (220, 38, 38)
        assert "? p1-b text" in texts
        assert "p1-a text" in texts
        assert not any(text.startswith("1.") for text in texts)
    finally:
        close(images)


def test_cross_page_edges_are_not_drawn_and_actual_frame_numbers_are_keys():
    images = DocumentReadingOrderRenderer(show_labels=False).render(
        image_source(frames=2), layout(numbers=(1, 2)), order((("p1-b", "p2-a"),))
    )
    try:
        assert list(images) == [1, 2]
        assert all(
            image.getpixel((30, 60)) == (255, 255, 255) for image in images.values()
        )
    finally:
        close(images)
    selected = DocumentReadingOrderRenderer().render(
        image_source(frames=2), layout(numbers=(2,)), order((("p2-a", "p2-b"),))
    )
    try:
        assert list(selected) == [2]
    finally:
        close(selected)


@pytest.mark.parametrize(
    "dpi,expected", [(72, (300, 400)), (96, (400, 534)), (144, (600, 800))]
)
def test_pdf_passwords_and_fractional_render_scale(make_pdf, dpi, expected):
    pytest.importorskip("pypdfium2")
    source = Artifact(
        data=make_pdf(password="example-password"), password="example-password"
    )
    images = DocumentReadingOrderRenderer(dpi=dpi, show_labels=False).render(
        source, layout(pdf=True, width=300, height=400), order()
    )
    try:
        assert images[1].size == expected
        assert images[1].getpixel(
            (int(30 * expected[0] / 300), int(60 * expected[1] / 400))
        ) == (148, 163, 184)
    finally:
        close(images)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("unit", [1, 2])
def test_reuses_crop_rotation_nonzero_origin_and_user_unit(make_pdf, rotation, unit):
    pytest.importorskip("pypdfium2")
    width, height = (250, 320) if rotation in (0, 180) else (320, 250)
    images = DocumentReadingOrderRenderer(dpi=72, show_labels=False).render(
        make_pdf(
            media=(10, 20, 310, 420),
            crop=(30, 60, 280, 380),
            rotation=rotation,
            unit=unit,
        ),
        layout(pdf=True, width=width * unit, height=height * unit),
        order(),
    )
    try:
        assert images[1].size == (width * unit, height * unit)
        assert images[1].getpixel((30, 60)) == (148, 163, 184)
    finally:
        close(images)


@pytest.mark.parametrize(
    "sequence", [order((("missing", "p1-a"),)), order((), unresolved=("missing",))]
)
def test_unknown_references_fail_before_source_is_opened(sequence):
    with pytest.raises(ValueError, match="absent"):
        DocumentReadingOrderRenderer().render("not-opened.pdf", layout(), sequence)


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_order_is_not_presented_as_a_valid_map(outcome):
    sequence = ReadingOrderResult(
        outcome=outcome,
        findings=(LayoutFinding(code="example.failure", message="No usable order."),),
    )
    with pytest.raises(ValueError, match="usable"):
        DocumentReadingOrderRenderer().render("not-opened.pdf", layout(), sequence)


def test_wrong_types_and_dimensions_are_rejected():
    renderer = DocumentReadingOrderRenderer()
    with pytest.raises(TypeError, match="layout"):
        renderer.render(b"", None, order())
    with pytest.raises(TypeError, match="order"):
        renderer.render(b"", layout(), None)
    with pytest.raises(ValueError, match="dimensions"):
        renderer.render(image_source(), layout(width=101), order())
    with pytest.raises(ValueError, match="max_pixels"):
        DocumentReadingOrderRenderer(max_pixels_per_page=100).render(
            image_source(), layout(), order()
        )


def test_blank_pdf_pages_are_supported(make_pdf):
    pytest.importorskip("pypdfium2")
    images = DocumentReadingOrderRenderer(dpi=72).render(
        make_pdf(), pdf_layout(blank=True), order(())
    )
    try:
        assert list(images) == [1]
        assert images[1].getpixel((30, 60)) == (255, 255, 255)
    finally:
        close(images)


def test_drawing_failure_closes_all_prepared_images(monkeypatch):
    captured = {}
    original = DocumentRegionRenderer.render

    def capture(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        captured.update(result)
        return result

    def fail(*args, **kwargs):
        raise RuntimeError("fixture drawing failure")

    monkeypatch.setattr(DocumentRegionRenderer, "render", capture)
    renderer = DocumentReadingOrderRenderer()
    monkeypatch.setattr(renderer, "_draw_order", fail)
    with pytest.raises(RuntimeError, match="fixture"):
        renderer.render(image_source(frames=2), layout(numbers=(1, 2)), order())
    assert len(captured) == 2
    for image in captured.values():
        with pytest.raises(ValueError, match="closed"):
            image.getpixel((0, 0))


def test_import_loads_models_but_not_optional_runtimes_or_notebook_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from raggae.documents.utilities import DocumentReadingOrderRenderer
assert 'raggae.documents.layout_analysis' in sys.modules
assert not any(name in sys.modules for name in (
    'pypdfium2', 'IPython', 'paddleocr', 'paddle', 'torch', 'transformers',
))
""",
        ],
        check=True,
    )
