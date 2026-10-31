"""Encoded image metadata removal, pixel interpretation, and shared I/O contract."""

from io import BytesIO

import pytest
from PIL import Image, ImageCms, PngImagePlugin
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    ImageMetadataFinding,
    ImageMetadataIssue,
    NormalizationWorker,
    ImageMetadataReport,
    ImageMetadataSanitizer,
    NormalizationRequest,
    NormalizationResult,
    PillowImageOrientationNormalizer,
)

ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">Private metadata</x:xmpmeta>'


def pixels(mode="RGB"):
    image = Image.new("RGB", (3, 2))
    image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)])
    converted = image.convert(mode)
    image.close()
    return converted


def encoded(image_format="PNG", *, metadata=True, icc=True, mode="RGB", orientation=None):
    with pixels(mode) as image, BytesIO() as output:
        options = {}
        if icc:
            options["icc_profile"] = ICC
        exif = Image.Exif()
        if metadata:
            exif[315] = "Private author"
            exif[271] = "Private camera"
            if image_format == "TIFF":
                exif[700] = XMP
            elif image_format == "JPEG":
                options["xmp"] = XMP
                options["comment"] = b"Private comment"
            else:
                text = PngImagePlugin.PngInfo()
                text.add_text("Description", "Private description")
                text.add_itxt("XML:com.adobe.xmp", XMP.decode())
                options["pnginfo"] = text
        if orientation is not None:
            exif[274] = orientation
        if exif:
            options["exif"] = exif
        image.save(output, format=image_format, **options)
        return output.getvalue()


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_selected_metadata_removed_and_colour_profile_preserved(image_format, source_kind, disk, tmp_path):
    original = encoded(image_format)
    source = original
    if source_kind == "path":
        path = tmp_path / "input.bin"
        path.write_bytes(original)
        source = path
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "clean.bin" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = ImageMetadataSanitizer().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.image_format == image_format
    assert result.report.dimensions == (3, 2)
    expected = ("exif", "xmp", "comment") if image_format == "JPEG" else ("exif", "xmp", "png_text") if image_format == "PNG" else ("exif", "xmp")
    assert result.report.removed_categories == expected
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    with Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        actual.load()
        assert actual.format == image_format
        assert actual.size == (3, 2)
        assert actual.mode == "RGB"
        assert actual.info["icc_profile"] == ICC
        assert 315 not in actual.getexif()
        assert 271 not in actual.getexif()
        assert 700 not in actual.getexif()
        assert "comment" not in actual.info
        assert "xmp" not in actual.info
        if image_format != "TIFF":
            assert not actual.getexif()
        if image_format == "PNG":
            assert actual.text == {}
        if image_format != "JPEG":
            with pixels() as expected_pixels:
                assert actual.tobytes() == expected_pixels.tobytes()
    assert "Private" not in result.report.model_dump_json()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert ImageMetadataReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
@pytest.mark.parametrize("icc", [False, True])
def test_no_private_metadata_is_exact_noop_including_structural_tiff_tags(image_format, icc, tmp_path):
    artifact = Artifact(data=encoded(image_format, metadata=False, icc=icc))
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=artifact, output_path=tmp_path / "unused"))
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert result.report.removed_categories == ()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
def test_idempotent_after_metadata_removal(image_format):
    worker = ImageMetadataSanitizer()
    first = worker.normalize(NormalizationRequest(source=encoded(image_format), output_path=None))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact


@pytest.mark.parametrize("mode", ["L", "RGBA", "P", "I;16"])
@pytest.mark.parametrize("image_format", ["PNG", "TIFF"])
def test_pixel_mode_preserved(mode, image_format):
    source = encoded(image_format, mode=mode)
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.outcome == "changed"
    with Image.open(BytesIO(source)) as original, Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        original.load()
        actual.load()
        assert actual.mode == original.mode
        assert actual.tobytes() == original.tobytes()


def test_png_palette_and_transparency_preserved():
    with pixels("P") as image, BytesIO() as output:
        text = PngImagePlugin.PngInfo()
        text.add_text("Author", "Private")
        image.save(output, format="PNG", pnginfo=text, transparency=0, icc_profile=ICC)
        source = output.getvalue()
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.report.removed_categories == ("png_text",)
    with Image.open(BytesIO(source)) as original, Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert actual.info["transparency"] == original.info["transparency"]
        assert actual.convert("RGBA").tobytes() == original.convert("RGBA").tobytes()
        assert actual.info["icc_profile"] == ICC


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
def test_orientation_worker_should_run_before_stripping_exif(image_format):
    source = encoded(image_format, orientation=6)
    oriented = PillowImageOrientationNormalizer().normalize(NormalizationRequest(source=source, output_path=None))
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=oriented.artifact, output_path=None))
    assert result.outcome == "changed"
    assert result.report.dimensions == (2, 3)
    with Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert actual.size == (2, 3)
        assert 274 not in actual.getexif()
        assert actual.info["icc_profile"] == ICC


def test_direct_png_sanitization_does_not_apply_orientation():
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=encoded(orientation=6), output_path=None))
    with Image.open(BytesIO(result.artifact.data)) as actual:
        assert actual.size == (3, 2)
        assert 274 not in actual.getexif()


def test_png_xmp_only_is_counted_once():
    with pixels() as image, BytesIO() as output:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_itxt("XML:com.adobe.xmp", XMP.decode())
        image.save(output, format="PNG", pnginfo=metadata)
        source = output.getvalue()
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.report.removed_categories == ("xmp",)


def test_gps_exif_removed():
    with pixels() as image, BytesIO() as output:
        exif = Image.Exif()
        exif[34853] = {1: "N", 2: (1.0, 2.0, 3.0), 3: "E", 4: (4.0, 5.0, 6.0)}
        image.save(output, format="JPEG", exif=exif)
        source = output.getvalue()
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.report.removed_categories == ("exif",)
    with Image.open(BytesIO(result.artifact.data)) as actual:
        assert 34853 not in actual.getexif()


@pytest.mark.parametrize("data", [b"", b"not an image", encoded("JPEG")[:-50]])
def test_decode_failure_leaves_no_output(data, tmp_path):
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=data, output_path=tmp_path / "output"))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageMetadataFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_unsupported_image():
    with pixels() as image, BytesIO() as output:
        image.save(output, format="GIF")
        source = output.getvalue()
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageMetadataFinding.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("image_format", ["PNG", "TIFF"])
def test_multiple_frames_not_dropped(image_format, tmp_path):
    with pixels() as image, pixels() as second, BytesIO() as output:
        image.save(output, format=image_format, save_all=True, append_images=[second])
        source = output.getvalue()
    result = ImageMetadataSanitizer().normalize(NormalizationRequest(source=source, output_path=tmp_path / "output"))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageMetadataFinding.MULTIPLE_FRAMES
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error", [ValueError("private encoder diagnostics"), OSError("disk full")])
def test_partial_output_failure_cleanup(error, monkeypatch, tmp_path):
    source = encoded()

    def broken_save(self, output, **kwargs):
        output.write(b"partial image")
        raise error

    monkeypatch.setattr(Image.Image, "save", broken_save)
    request = NormalizationRequest(source=source, output_path=tmp_path / "output")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            ImageMetadataSanitizer().normalize(request)
        assert caught.value is error
    else:
        result = ImageMetadataSanitizer().normalize(request)
        assert result.artifact is None
        assert result.report.issues[0].code == ImageMetadataFinding.ENCODE_FAILED
        assert result.report.removed_categories == ()
        assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_existing_destination_preserved(tmp_path):
    path = tmp_path / "existing"
    source = encoded()
    path.write_bytes(source)
    with pytest.raises(FileExistsError):
        ImageMetadataSanitizer().normalize(NormalizationRequest(source=path, output_path=path))
    assert path.read_bytes() == source


def test_worker_contract_and_unused_request_settings():
    worker = ImageMetadataSanitizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_image_metadata"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})
    result = worker.normalize(NormalizationRequest(source=Artifact(data=encoded(), password="unused-secret"), output_path=None,  declared_encoding="ascii"))
    assert result.outcome == "changed"
    assert "unused-secret" not in repr(result)


def test_report_read_only_and_serializable():
    report = ImageMetadataReport(outcome="changed", image_format="PNG", dimensions=(3, 2), removed_categories=["exif"])
    assert report.removed_categories == ("exif",)
    assert ImageMetadataReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.dimensions = (1, 1)
    with pytest.raises(ValidationError):
        ImageMetadataIssue(code=ImageMetadataFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "no_change"},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (3, 2)},
    {"outcome": "no_change", "image_format": "PNG", "dimensions": (3, 2), "removed_categories": ["exif"]},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (3, 2), "removed_categories": ["exif", "exif"]},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (3, 2), "removed_categories": ["unknown"]},
    {"outcome": "no_change", "image_format": "PNG", "dimensions": (0, 2)},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        ImageMetadataReport(**kwargs)
