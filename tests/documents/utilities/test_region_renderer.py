"""Returned image pixels, coordinate transforms, source preservation, and cleanup."""

from io import BytesIO
import subprocess
import sys

import pikepdf
from PIL import Image, ImageChops
import pytest

from raggae.documents.layout_analysis import (
    DocxBodyLocation,
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutFinding,
    LayoutRegion,
    LayoutSurface,
    PdfPageLocation,
    PptxSlideLocation,
    XlsxSheetLocation,
)
from raggae.documents.normalization import Artifact
from ..layout_analysis.test_visual import raster
from raggae.documents.utilities import DocumentRegionRenderer


def pdf_layout(
    *,
    numbers=(1,),
    width=300,
    height=400,
    kind="text",
    box=(20, 40, 100, 120),
    blank=False,
):
    surfaces = tuple(
        LayoutSurface(
            id_=f"page-{n}",
            location=PdfPageLocation(page_number=n),
            width=width,
            height=height,
        )
        for n in numbers
    )
    regions = (
        ()
        if blank
        else tuple(
            LayoutRegion(
                id=f"page-{n}-r1",
                surface_id=f"page-{n}",
                kind=kind,
                bbox=box,
            )
            for n in numbers
        )
    )
    return LayoutAnalysisResult(outcome="success", surfaces=surfaces, regions=regions)


def image_layout(
    *, frames=(1,), width=80, height=60, kind="text", box=(20, 20, 60, 50)
):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=tuple(
            LayoutSurface(
                id_=f"image-{n}",
                kind="image",
                location=ImageFrameLocation(frame_number=n),
                width=width,
                height=height,
            )
            for n in frames
        ),
        regions=tuple(
            LayoutRegion(
                id=f"image-{n}-r1",
                surface_id=f"image-{n}",
                kind=kind,
                bbox=box,
            )
            for n in frames
        ),
    )


def close(images):
    for image in images.values():
        image.close()


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "file_artifact", "memory_artifact"]
)
def test_pdf_outputs_are_detached_saveable_images_and_source_is_unchanged(
    make_pdf, source_kind, tmp_path
):
    pytest.importorskip("pypdfium2")
    data = make_pdf()
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "file_artifact": Artifact(path=path),
        "memory_artifact": Artifact(data=data),
    }[source_kind]
    layout = pdf_layout()
    snapshot = layout.model_dump_json()
    images = DocumentRegionRenderer(dpi=72, show_labels=False).render(source, layout)
    try:
        assert list(images) == [1]
        image = images[1]
        assert image.mode == "RGB" and image.size == (300, 400)
        assert image.getpixel((20, 60)) == (37, 99, 235)
        assert image.getpixel((150, 200)) == (255, 255, 255)
        with BytesIO() as saved:
            image.save(saved, format="PNG")
            saved.seek(0)
            with Image.open(saved) as reopened:
                assert reopened.size == image.size
        assert path.read_bytes() == data
        assert layout.model_dump_json() == snapshot
        assert list(tmp_path.iterdir()) == [path]
    finally:
        close(images)


@pytest.mark.parametrize(
    "kind,color",
    [
        ("text", (37, 99, 235)),
        ("heading", (190, 24, 93)),
        ("caption", (15, 118, 110)),
        ("table", (21, 128, 61)),
        ("image", (194, 65, 12)),
        ("unknown", (124, 58, 237)),
    ],
)
def test_every_region_kind_has_a_color(kind, color):
    images = DocumentRegionRenderer(show_labels=False).render(
        raster(), image_layout(kind=kind)
    )
    try:
        assert images[1].size == (80, 60)
        assert images[1].getpixel((20, 30)) == color
    finally:
        close(images)


def test_labels_are_optional_and_full_surface_boxes_keep_the_right_border():
    data = raster(size=(300, 200))
    layout = image_layout(width=300, height=200, box=(20, 80, 200, 160))
    plain = DocumentRegionRenderer(show_labels=False).render(data, layout)
    labelled = DocumentRegionRenderer().render(data, layout)
    try:
        with ImageChops.difference(plain[1], labelled[1]) as difference:
            assert difference.getbbox() is not None
    finally:
        close(plain)
        close(labelled)
    images = DocumentRegionRenderer(show_labels=False).render(
        raster(), image_layout(box=(0, 0, 80, 60))
    )
    try:
        assert images[1].getpixel((79, 59)) == (37, 99, 235)
    finally:
        close(images)


@pytest.mark.parametrize(
    "rotation,box",
    [
        (0, (20, 40, 60, 80)),
        (90, (240, 20, 280, 60)),
        (180, (190, 240, 230, 280)),
        (270, (40, 190, 80, 230)),
    ],
)
@pytest.mark.parametrize("unit", [1, 2])
def test_overlay_tracks_actual_render_crop_rotation_origin_and_unit(
    make_pdf, rotation, box, unit
):
    pytest.importorskip("pypdfium2")
    data = make_pdf(
        (b"1 0 0 rg 50 300 40 40 re f",),
        media=(10, 20, 310, 420),
        crop=(30, 60, 280, 380),
        rotation=rotation,
        unit=unit,
    )
    width, height = (250, 320) if rotation in (0, 180) else (320, 250)
    scaled = tuple(v * unit for v in box)
    images = DocumentRegionRenderer(dpi=72, show_labels=False).render(
        data,
        pdf_layout(width=width * unit, height=height * unit, kind="table", box=scaled),
    )
    try:
        image = images[1]
        assert image.size == (width * unit, height * unit)
        left, top, right, bottom = scaled
        assert image.getpixel((int(left), int((top + bottom) / 2))) == (21, 128, 61)
        assert image.getpixel((int((left + right) / 2), int((top + bottom) / 2))) == (
            255,
            0,
            0,
        )
    finally:
        close(images)


@pytest.mark.parametrize(
    "dpi,size,left", [(72, (300, 400), 20), (96, (400, 534), 26), (144, (600, 800), 40)]
)
def test_pdf_dpi_scales_images_and_boxes(make_pdf, dpi, size, left):
    pytest.importorskip("pypdfium2")
    images = DocumentRegionRenderer(dpi=dpi, show_labels=False).render(
        make_pdf(), pdf_layout()
    )
    try:
        assert images[1].size == size
        assert images[1].getpixel((left, int(60 * size[1] / 400))) == (
            37,
            99,
            235,
        )
    finally:
        close(images)


def test_only_mapped_pages_are_rendered_with_their_actual_numbers(make_pdf):
    pytest.importorskip("pypdfium2")
    data = make_pdf((b"", b"", b"0 1 0 rg 0 0 300 400 re f"))
    images = DocumentRegionRenderer(dpi=72).render(
        data, pdf_layout(numbers=(3,), blank=True)
    )
    try:
        assert list(images) == [3]
        assert images[3].getpixel((150, 200)) == (0, 255, 0)
    finally:
        close(images)


def test_pdf_password_is_taken_from_the_artifact(make_pdf):
    pytest.importorskip("pypdfium2")
    data = make_pdf(password="fixture-secret")
    with pytest.raises(pikepdf.PasswordError):
        DocumentRegionRenderer().render(data, pdf_layout())
    images = DocumentRegionRenderer().render(
        Artifact(data=data, password="fixture-secret"), pdf_layout()
    )
    close(images)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "TIFF", "WEBP", "BMP", "GIF"])
def test_image_formats_work_without_pdf_renderer(format):
    images = DocumentRegionRenderer().render(raster(format), image_layout())
    try:
        assert images[1].mode == "RGB"
        assert images[1].size == (80, 60)
    finally:
        close(images)


@pytest.mark.parametrize(
    "format,size,orientation", [("JPEG", (80, 60), 6), ("TIFF", (60, 80), 6)]
)
def test_image_orientation_matches_the_decoded_frame(format, size, orientation):
    images = DocumentRegionRenderer().render(
        raster(format, orientation=orientation),
        image_layout(width=size[0], height=size[1]),
    )
    try:
        assert images[1].size == size
    finally:
        close(images)


def test_tiff_frames_are_independent_and_transparency_is_white():
    images = DocumentRegionRenderer().render(
        raster("TIFF", frames=2), image_layout(frames=(1, 2))
    )
    try:
        assert list(images) == [1, 2]
        assert images[1] is not images[2]
    finally:
        close(images)
    images = DocumentRegionRenderer().render(
        raster(mode="RGBA", color=(0, 0, 0, 0)), image_layout()
    )
    try:
        assert images[1].getpixel((0, 0)) == (255, 255, 255)
    finally:
        close(images)


@pytest.mark.parametrize("field", ["dpi", "max_pixels_per_page"])
@pytest.mark.parametrize(
    "value,error",
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)],
)
def test_invalid_rendering_options_fail_before_io(field, value, error):
    with pytest.raises(error):
        DocumentRegionRenderer(**{field: value}).render("missing.pdf", pdf_layout())


@pytest.mark.parametrize(
    "kind,location,dimensions",
    [
        ("flow", DocxBodyLocation(part="word/document.xml"), {}),
        ("sheet", XlsxSheetLocation(sheet_number=1, sheet_name="Sheet"), {}),
        (
            "slide",
            PptxSlideLocation(slide_number=1, part="ppt/slides/slide1.xml"),
            {"width": 300, "height": 400},
        ),
    ],
)
def test_native_unrendered_formats_are_rejected(kind, location, dimensions):
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(LayoutSurface(id_="s", kind=kind, location=location, **dimensions),),
    )
    with pytest.raises(ValueError, match="PDF-page or image-frame"):
        DocumentRegionRenderer().render("not-opened", layout)


def test_empty_maps_invalid_inputs_and_dimension_mismatches():
    failed = LayoutAnalysisResult(
        outcome="failed",
        findings=(LayoutFinding(code="test.failed", message="No map."),),
    )
    with pytest.raises(ValueError, match="usable"):
        DocumentRegionRenderer().render(b"", failed)
    with pytest.raises(TypeError, match="layout"):
        DocumentRegionRenderer().render(b"", {})
    with pytest.raises(ValueError, match="dimensions"):
        DocumentRegionRenderer().render(raster(), image_layout(width=100))
    with pytest.raises(ValueError, match="frame"):
        DocumentRegionRenderer().render(raster(), image_layout(frames=(2,)))


def test_render_limits_and_pdf_geometry_mismatch(make_pdf):
    pytest.importorskip("pypdfium2")
    with pytest.raises(ValueError, match="dimensions"):
        DocumentRegionRenderer().render(make_pdf(), pdf_layout(width=301))
    with pytest.raises(ValueError, match="max_pixels"):
        DocumentRegionRenderer(max_pixels_per_page=10).render(make_pdf(), pdf_layout())
    with pytest.raises(ValueError, match="max_pixels"):
        DocumentRegionRenderer(max_pixels_per_page=10).render(raster(), image_layout())


def test_failure_closes_already_prepared_images(make_pdf, monkeypatch):
    pytest.importorskip("pypdfium2")
    original = DocumentRegionRenderer._draw_regions
    prepared = []

    def track(*args, **kwargs):
        image = original(*args, **kwargs)
        prepared.append(image)
        return image

    monkeypatch.setattr(DocumentRegionRenderer, "_draw_regions", track)
    with pytest.raises(ValueError, match="referenced PDF page"):
        DocumentRegionRenderer().render(
            make_pdf(), pdf_layout(numbers=(1, 99), blank=True)
        )
    assert len(prepared) == 1
    with pytest.raises(ValueError):
        prepared[0].getpixel((0, 0))


def test_public_import_loads_layout_models_but_not_optional_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from raggae.documents.utilities import DocumentRegionRenderer
assert 'raggae.documents.layout_analysis' in sys.modules
assert not any(name in sys.modules for name in (
    'pypdfium2', 'IPython', 'paddleocr', 'paddle', 'torch', 'transformers',
))
""",
        ],
        check=True,
    )


def test_layout_and_generic_calls_produce_identical_pixels(make_pdf):
    pytest.importorskip("pypdfium2")
    data = make_pdf()
    renderer = DocumentRegionRenderer()
    from_layout = renderer.render(data, layout=pdf_layout())
    generic = renderer.render(
        data,
        boxes={1: [(20, 40, 100, 120)]},
        labels={1: ["page-1-r1 text"]},
        colors={1: ["#2563EB"]},
    )
    try:
        assert from_layout[1].tobytes() == generic[1].tobytes()
    finally:
        close(from_layout)
        close(generic)


def test_layout_cannot_be_mixed_with_generic_parameters_or_duplicate_page_numbers():
    renderer = DocumentRegionRenderer()
    layout = pdf_layout()
    with pytest.raises(TypeError, match="not both"):
        renderer.render("not-opened.pdf", layout=layout, boxes={1: []})
    duplicate = layout.surfaces[0].model_copy(update={"id_": "another-id"})
    layout = layout.model_copy(update={"surfaces": layout.surfaces + (duplicate,)})
    with pytest.raises(ValueError, match="more than once"):
        renderer.render("not-opened.pdf", layout=layout)
