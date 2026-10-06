import struct
import warnings
import zlib

from PIL import Image, UnidentifiedImageError
import pytest

from raggae.documents.validation import ImageLimits, ImageResourceProbe
from raggae.documents.validation.checks.resource_limits import image as image_module
from raggae.documents.validation.checks.resource_limits.policy import ResourceFinding as Code


@pytest.fixture
def image_factory(tmp_path):
    def make_image(name="image.png", *, format_name="PNG", sizes=((2, 3),)):
        path = tmp_path / name
        frames = [Image.new("RGB", size, color=(index * 40, 80, 120)) for index, size in enumerate(sizes)]
        try:
            if len(frames) > 1:
                frames[0].save(path, format=format_name, save_all=True, append_images=frames[1:])
            else:
                frames[0].save(path, format=format_name)
        finally:
            for frame in frames:
                frame.close()
        return path

    return make_image


@pytest.mark.parametrize("backend,detected", [("PNG", "png"), ("JPEG", "jpeg"), ("JPEG", " .JPG "),
                                             ("TIFF", "tiff"), ("TIFF", " .TIF ")])
@pytest.mark.parametrize("as_string", [False, True])
def test_real_formats_aliases_and_exact_facts(image_factory, backend, detected, as_string):
    path = image_factory(format_name=backend)
    before = path.read_bytes()
    outcome = ImageResourceProbe().check(str(path) if as_string else path, detected)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"image_width": 2, "image_height": 3, "image_pixels": 6, "frame_count": 1}
    assert path.read_bytes() == before


@pytest.mark.parametrize("backend", ["PNG", "JPEG", "TIFF"])
@pytest.mark.parametrize("size", [(1, 2), (1, 3), (2, 2)])
def test_pixel_boundary_without_decoding(image_factory, monkeypatch, forbidden_operation, backend, size):
    path = image_factory(format_name=backend, sizes=[size])
    for method in ("load", "convert", "tobytes"):
        monkeypatch.setattr(Image.Image, method, forbidden_operation)
    outcome = ImageResourceProbe(ImageLimits(max_pixels=3)).check(path)
    pixels = size[0] * size[1]
    assert outcome.codes == ((Code.TOO_MANY_PIXELS,) if pixels > 3 else ())
    assert outcome.facts["image_pixels"] == pixels
    if pixels > 3:
        assert dict(outcome.findings[0].detail) == {"limit": 3, "observed": 4}


@pytest.mark.parametrize("frames", [1, 2, 3])
def test_real_tiff_frame_boundary_without_decoding(image_factory, monkeypatch, forbidden_operation, frames):
    path = image_factory(format_name="TIFF", sizes=[(2, 2)] * frames)
    monkeypatch.setattr(Image.Image, "load", forbidden_operation)
    outcome = ImageResourceProbe(ImageLimits(max_frames=2)).check(path, "tiff")
    assert outcome.codes == ((Code.TOO_MANY_FRAMES,) if frames > 2 else ())
    assert dict(outcome.facts) == {"image_width": 2, "image_height": 2, "image_pixels": 4, "frame_count": frames}
    if frames > 2:
        assert dict(outcome.findings[0].detail) == {"limit": 2, "observed": 3}


def test_pixel_and_frame_excess_reported_together(image_factory):
    path = image_factory(format_name="TIFF", sizes=[(2, 2)] * 3)
    outcome = ImageResourceProbe(ImageLimits(max_pixels=3, max_frames=2)).check(path)
    assert outcome.codes == (Code.TOO_MANY_PIXELS, Code.TOO_MANY_FRAMES)
    assert [dict(f.detail) for f in outcome.findings] == [{"limit": 3, "observed": 4}, {"limit": 2, "observed": 3}]


def test_later_frame_dimensions_are_a_documented_limit_not_a_total_budget(image_factory):
    path = image_factory(format_name="TIFF", sizes=[(1, 1), (4, 4)])
    with Image.open(path) as image:
        image.seek(1)
        assert image.size == (4, 4)
    outcome = ImageResourceProbe(ImageLimits(max_pixels=2, max_frames=2)).check(path)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"image_width": 1, "image_height": 1, "image_pixels": 1, "frame_count": 2}


@pytest.mark.parametrize("password", [None, "", "irrelevant"])
def test_password_is_irrelevant(image_factory, password):
    path = image_factory()
    assert ImageResourceProbe().check(path, password=password) == ImageResourceProbe().check(path)


def test_huge_declared_png_header_is_refused_without_allocating_pixels(image_factory, monkeypatch, forbidden_operation):
    path = image_factory(sizes=[(1, 1)])
    content = bytearray(path.read_bytes())
    assert content[12:16] == b"IHDR"
    struct.pack_into(">II", content, 16, 60_000, 60_000)
    struct.pack_into(">I", content, 29, zlib.crc32(content[12:29]) & 0xFFFFFFFF)
    path.write_bytes(content)
    before = path.read_bytes()
    previous_filters = warnings.filters[:]
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    for method in ("load", "convert", "tobytes"):
        monkeypatch.setattr(Image.Image, method, forbidden_operation)
    original_open = Image.open
    opened = []

    def open_header(*args, **kwargs):
        assert Image.MAX_IMAGE_PIXELS is None
        image = original_open(*args, **kwargs)
        opened.append(image)
        return image

    monkeypatch.setattr(Image, "open", open_header)
    outcome = ImageResourceProbe().check(path, "png")
    assert outcome.codes == (Code.TOO_MANY_PIXELS,)
    assert dict(outcome.facts) == {"image_width": 60_000, "image_height": 60_000,
                                 "image_pixels": 3_600_000_000, "frame_count": 1}
    assert dict(outcome.findings[0].detail) == {"limit": 100_000_000, "observed": 3_600_000_000}
    assert Image.MAX_IMAGE_PIXELS == 1
    assert warnings.filters == previous_filters
    assert len(opened) == 1 and opened[0].fp is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("stage", ["open", "size", "frames"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, UnidentifiedImageError])
def test_read_errors_close_and_restore_global_state(monkeypatch, missing_path, stage, error_type):
    class BrokenImage:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        @property
        def size(self):
            if stage == "size":
                raise error_type("metadata failure")
            return (2, 3)

        @property
        def n_frames(self):
            raise error_type("metadata failure")

    image = BrokenImage()

    def open_image(*args, **kwargs):
        assert Image.MAX_IMAGE_PIXELS is None
        if stage == "open":
            raise error_type("metadata failure")
        return image

    monkeypatch.setattr(image_module.Image, "open", open_image)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 77)
    previous_filters = warnings.filters[:]
    outcome = ImageResourceProbe().check(missing_path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": missing_path.name}
    assert image.closed == (stage != "open")
    assert Image.MAX_IMAGE_PIXELS == 77
    assert warnings.filters == previous_filters


def test_single_frame_fallback_when_backend_lacks_n_frames(monkeypatch, missing_path):
    class Header:
        size = (2, 3)
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    header = Header()
    monkeypatch.setattr(Image, "open", lambda *a, **k: header)
    outcome = ImageResourceProbe().check(missing_path)
    assert outcome.codes == ()
    assert outcome.facts["frame_count"] == 1
    assert header.closed


@pytest.mark.parametrize("format_name,normalized", [("pdf", "pdf"), ("gif", "gif"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(Image, "open", forbidden_operation)
    outcome = ImageResourceProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("bad_path", [None, 1, b"image.png", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        ImageResourceProbe().check(bad_path)


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_and_corrupt_image_are_unreadable(file_factory, missing_path, kind):
    path = missing_path if kind == "missing" else file_factory("broken.png", b"not an image")
    outcome = ImageResourceProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


def test_repeated_calls_and_policy_are_instance_local(image_factory):
    probe = ImageResourceProbe(ImageLimits(max_pixels=3))
    large = image_factory("large.png", sizes=[(2, 2)])
    small = image_factory("small.png", sizes=[(1, 1)])
    assert probe.check(large).codes == (Code.TOO_MANY_PIXELS,)
    assert probe.check(small).codes == ()
    assert probe.check(large).facts["image_pixels"] == 4
    assert ImageResourceProbe().check(large).codes == ()
    assert probe.name == "validation_resource_limits_image"
    assert probe.handles == frozenset({"png", "jpeg", "tiff"})
