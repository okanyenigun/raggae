"""Frames of PNG, JPEG, and TIFF files with Pillow, or crops of their layout regions."""

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    PillowImageExtractor,
)
from raggae.documents.layout_analysis import (
    ImageFrameLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import make_layout

RED, BLUE, GREEN = (200, 30, 30), (30, 30, 200), (30, 160, 30)


def save(frames, format, **options):
    first, *rest = frames
    with BytesIO() as output:
        first.save(output, format, save_all=bool(rest), append_images=rest, **options)
        return output.getvalue()


def codes(result):
    return [(f.code, f.page_index, f.region_id) for f in result.findings]


def summary(result):
    return [
        (p.page_index, p.region_id, p.media_type, p.method, p.width, p.height)
        for p in result.images
    ]


def opened(picture):
    image = Image.open(BytesIO(picture.data))
    image.load()
    return image


def extract(data, **options):
    return PillowImageExtractor().extract(data, **options)


def frame_layout(*regions, frames=((100, 80),)):
    """Image surfaces of the given pixel sizes; regions are (id, frame, kind, bbox)."""
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=tuple(
            LayoutSurface(
                id_=f"frame-{number}",
                kind="image",
                location=ImageFrameLocation(frame_number=number),
                width=width,
                height=height,
            )
            for number, (width, height) in enumerate(frames, 1)
        ),
        regions=tuple(
            LayoutRegion(id=identifier, surface_id=f"frame-{frame}", kind=kind, bbox=bbox)
            for identifier, frame, kind, bbox in regions
        ),
    )


@pytest.fixture
def scan():
    """A two-page TIFF scan: page 1 holds a red and a blue figure, page 2 a green one."""
    first = Image.new("RGB", (100, 80), "white")
    ImageDraw.Draw(first).rectangle((10, 10, 39, 29), fill=RED)
    ImageDraw.Draw(first).rectangle((50, 40, 89, 69), fill=BLUE)
    second = Image.new("RGB", (100, 80), "white")
    ImageDraw.Draw(second).rectangle((20, 20, 59, 49), fill=GREEN)
    return save([first, second], "TIFF")


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import image

    assert image.PillowImageExtractor is PillowImageExtractor


@pytest.mark.parametrize(
    "source_kind", ["path", "string", "bytes", "artifact_path", "artifact_bytes"]
)
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = save([Image.new("RGB", (40, 30), RED)], "PNG")
    path = tmp_path / "photo.png"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact_path": Artifact(path=path, password="private-password"),
        "artifact_bytes": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = PillowImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_pillow"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [(0, None, "image/png", "original", 40, 30)]
    assert result.images[0].data == data
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Standalone: frames -------------------------------------------------------


@pytest.mark.parametrize(
    "image,format,media_type",
    [
        (Image.new("RGB", (40, 30), RED), "JPEG", "image/jpeg"),
        (Image.new("RGBA", (40, 30), (0, 0, 255, 128)), "PNG", "image/png"),
        (Image.new("CMYK", (40, 30), (0, 100, 200, 0)), "TIFF", "image/tiff"),
    ],
)
def test_a_single_frame_file_comes_back_byte_for_byte(image, format, media_type):
    data = save([image], format)
    result = extract(data)
    assert summary(result) == [(0, None, media_type, "original", 40, 30)]
    assert result.images[0].data == data


def test_each_frame_of_a_multi_page_tiff_is_a_new_file(scan):
    result = extract(scan)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, None, "image/png", "reencoded", 100, 80),
        (1, None, "image/png", "reencoded", 100, 80),
    ]
    assert opened(result.images[0]).getpixel((20, 20)) == RED
    assert opened(result.images[1]).getpixel((30, 30)) == GREEN


def test_frames_keep_cmyk_and_transparency():
    data = save(
        [
            Image.new("CMYK", (8, 6), (0, 100, 200, 10)),
            Image.new("RGBA", (8, 6), (0, 0, 255, 128)),
        ],
        "TIFF",
    )
    cmyk, rgba = extract(data).images
    assert cmyk.media_type == "image/tiff" and opened(cmyk).mode == "CMYK"
    assert rgba.media_type == "image/png"
    assert opened(rgba).getpixel((0, 0)) == (0, 0, 255, 128)


def test_an_animated_png_gives_one_picture_per_frame():
    data = save([Image.new("RGB", (6, 6), RED), Image.new("RGB", (6, 6), BLUE)], "PNG")
    result = extract(data)
    assert [(p.page_index, p.method) for p in result.images] == [
        (0, "reencoded"),
        (1, "reencoded"),
    ]
    assert opened(result.images[1]).getpixel((3, 3)) == BLUE


def test_exif_rotation_is_left_to_normalization():
    # A photo stored sideways with "rotate 90° when showing" (EXIF orientation 6).
    stored = Image.new("RGB", (40, 20), "white")
    ImageDraw.Draw(stored).rectangle((0, 0, 9, 19), fill=RED)
    exif = Image.Exif()
    exif[0x0112] = 6
    data = save([stored], "JPEG", exif=exif, quality=95)
    result = extract(data)
    assert result.images[0].data == data and not result.findings
    layout = frame_layout(("left", 1, "image", (0, 0, 10, 20)), frames=((40, 20),))
    [crop] = extract(data, layout=layout).images
    # Cut from the stored, unrotated pixels, with no finding.
    assert (crop.width, crop.height) == (10, 20)
    assert opened(crop).getpixel((5, 10))[0] > 150


# Problems -----------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        save([Image.new("P", (5, 5))], "GIF"),
        save([Image.new("RGB", (5, 5))], "BMP"),
        save([Image.new("RGB", (5, 5))], "WEBP"),
        b"plain text, not an image",
    ],
)
def test_other_formats_are_not_applicable(data):
    result = extract(data)
    assert result.outcome == "not_applicable"
    assert codes(result) == [("not_applicable", None, None)]


def test_empty_missing_and_directory_sources(tmp_path):
    assert codes(extract(b"")) == [("decode_failed", None, None)]
    for source in (tmp_path / "missing.png", tmp_path):
        assert codes(extract(source)) == [("source_unreadable", None, None)]


def test_a_truncated_image_cannot_be_decoded():
    data = save([Image.effect_noise((64, 64), 50).convert("RGB")], "PNG")
    result = extract(data[: len(data) // 2])
    assert result.outcome == "failed"
    assert codes(result) == [("page_unreadable", 0, None)]


def test_a_frame_that_cannot_be_decoded_keeps_the_others(scan, monkeypatch):
    from PIL import TiffImagePlugin

    real = TiffImagePlugin.TiffImageFile.load

    def flaky(self):
        if self.tell() == 0:
            raise OSError("private decoder detail")
        return real(self)

    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "load", flaky)
    result = extract(scan)
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [1]
    assert codes(result) == [("page_unreadable", 0, None)]
    assert "private" not in result.model_dump_json()


def test_pillows_decompression_bomb_ceiling(monkeypatch):
    small, large = Image.new("RGB", (4, 4), RED), Image.new("RGB", (100, 80), BLUE)
    # A copy for the PNG: Pillow keeps a saved image's PNG encoder settings on the
    # object, and appending that same object to a TIFF would then fail.
    first_large = save([large.copy()], "PNG")
    later_large = save([small, large], "TIFF")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)  # ceiling: 20 pixels
    # Pillow refuses to open a file whose first frame is too large...
    assert codes(extract(first_large)) == [("image_too_large", None, None)]
    # ...and a later frame is checked before it is decoded.
    with pytest.warns(Image.DecompressionBombWarning):
        result = extract(later_large)
    assert result.outcome == "partial"
    assert [p.page_index for p in result.images] == [0]
    assert codes(result) == [("image_too_large", 1, None)]


# Layout regions ------------------------------------------------------------

RED_FIGURE = ("red", 1, "image", (10, 10, 40, 30))
BLUE_FIGURE = ("blue", 1, "image", (50, 40, 90, 70))
GREEN_FIGURE = ("green", 2, "image", (20, 20, 60, 50))
CAPTION = ("caption", 1, "text", (10, 32, 90, 38))
BOTH = ("both", 1, "image", (0, 0, 100, 80))

TWO_FRAMES = ((100, 80), (100, 80))


def test_layout_crops_image_regions_on_their_frames(scan):
    layout = frame_layout(RED_FIGURE, BLUE_FIGURE, GREEN_FIGURE, CAPTION, frames=TWO_FRAMES)
    result = extract(scan, layout=layout)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        (0, "red", "image/png", "reencoded", 30, 20),
        (0, "blue", "image/png", "reencoded", 40, 30),
        (1, "green", "image/png", "reencoded", 40, 30),
    ]
    assert [opened(p).getpixel((5, 5)) for p in result.images] == [RED, BLUE, GREEN]


def test_explicit_regions_any_kind_and_overlaps(scan):
    layout = frame_layout(RED_FIGURE, CAPTION, BOTH, frames=TWO_FRAMES)
    result = extract(scan, layout=layout, region_ids=["caption", "red", "both"])
    assert [(p.region_id, p.width, p.height) for p in result.images] == [
        ("caption", 80, 6),
        ("red", 30, 20),
        ("both", 100, 80),
    ]


def test_crops_keep_transparency_and_cmyk():
    rgba = save([Image.new("RGBA", (20, 20), (0, 0, 255, 128))], "PNG")
    layout = frame_layout(("piece", 1, "image", (5, 5, 15, 15)), frames=((20, 20),))
    [piece] = extract(rgba, layout=layout).images
    assert piece.media_type == "image/png"
    assert opened(piece).getpixel((0, 0)) == (0, 0, 255, 128)
    cmyk = save([Image.new("CMYK", (20, 20), (0, 100, 200, 0))], "JPEG")
    [piece] = extract(cmyk, layout=layout).images
    assert piece.media_type == "image/tiff" and opened(piece).mode == "CMYK"


def test_regions_that_do_not_fit_the_source(scan):
    layout = LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            *frame_layout(frames=((100, 80), (100, 80), (100, 80))).surfaces,
            *make_layout().surfaces,
        ),
        regions=(
            LayoutRegion(id="red", surface_id="frame-1", kind="image", bbox=(10, 10, 40, 30)),
            LayoutRegion(id="gone", surface_id="frame-3", kind="image", bbox=(0, 0, 9, 9)),
            LayoutRegion(id="pdf", surface_id="page-1", kind="image", bbox=(0, 0, 9, 9)),
        ),
    )
    result = extract(scan, layout=layout)
    assert result.outcome == "partial"
    assert [p.region_id for p in result.images] == ["red"]
    assert codes(result) == [
        ("page_missing", 2, "gone"),
        ("selection_unsupported", None, "pdf"),
    ]
    smaller = extract(scan, layout=frame_layout(RED_FIGURE, frames=((90, 80),)))
    assert smaller.outcome == "failed"
    assert codes(smaller) == [("layout_mismatch", 0, "red")]


def test_nothing_selected_without_opening_the_file(tmp_path):
    missing = tmp_path / "missing.tiff"
    explicit = extract(missing, layout=frame_layout(RED_FIGURE), region_ids=[])
    no_images = extract(missing, layout=frame_layout(CAPTION))
    for result in (explicit, no_images):
        assert result.outcome == "success"
        assert codes(result) == [("nothing_selected", None, None)]
    assert explicit.findings[0].message != no_images.findings[0].message


@pytest.mark.parametrize(
    "options,error",
    [
        ({"region_ids": ["red"]}, ValueError),
        ({"layout": "layout"}, TypeError),
        ({"layout": "fixture", "region_ids": "red"}, TypeError),
        ({"layout": "fixture", "region_ids": ["nope"]}, ValueError),
    ],
)
def test_invalid_selection_arguments_raise(scan, options, error):
    if options.get("layout") == "fixture":
        options = {**options, "layout": frame_layout(RED_FIGURE)}
    with pytest.raises(error):
        extract(scan, **options)
