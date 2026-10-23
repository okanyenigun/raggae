"""Actual image bytes exercise orientation and the shared public contract."""

from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from raggae.documents.normalization import (
    Artifact,
    ImageOrientationFinding,
    ImageOrientationIssue,
    NormalizationWorker,
    ImageOrientationReport,
    NormalizationRequest,
    NormalizationResult,
    PillowImageOrientationNormalizer,
)


def pixels():
    image = Image.new("RGB", (3, 2))
    image.putdata([
        (255, 0, 0), (0, 255, 0), (0, 0, 255),
        (255, 255, 0), (255, 0, 255), (0, 255, 255),
    ])
    return image


def encoded(image_format="PNG", orientation=6, mode="RGB"):
    with pixels() as image:
        converted = image.convert(mode)
        with converted, BytesIO() as output:
            exif = Image.Exif()
            if orientation is not None:
                exif[274] = orientation
            exif[315] = "Author retained"
            options = {}
            if image_format == "PNG":
                info = PngImagePlugin.PngInfo()
                info.add_text("Description", "Keep this text")
                options["pnginfo"] = info
            converted.save(output, format=image_format, exif=exif, **options)
            return output.getvalue()


OPERATIONS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


@pytest.mark.parametrize("image_format", ["PNG", "TIFF", "JPEG"])
@pytest.mark.parametrize("orientation", range(2, 9))
@pytest.mark.parametrize("on_disk", [False, True])
def test_all_transforms(image_format, orientation, on_disk, tmp_path):
    data = encoded(image_format, orientation)
    source = Artifact(data=data)
    if on_disk:
        path = tmp_path / "input.bin"  # Identify bytes, not the extension.
        path.write_bytes(data)
        source = Artifact(path=path)
    destination = tmp_path / "result.bin" if on_disk else None
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=source, output_path=destination)
    )
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.source_orientation == orientation
    assert result.report.input_dimensions == (3, 2)
    assert result.artifact is not None
    assert result.artifact.path == destination
    assert source.read_bytes() == data
    with Image.open(BytesIO(result.artifact.read_bytes())) as actual:
        actual.load()
        assert actual.format == image_format
        assert actual.getexif().get(274) in (None, 1)
        assert actual.getexif().get(315) == "Author retained"
        with pixels() as original, original.transpose(OPERATIONS[orientation]) as expected:
            assert actual.size == expected.size == result.report.output_dimensions
            if image_format != "JPEG":  # JPEG re-encoding is inherently lossy.
                assert actual.tobytes() == expected.tobytes()
        if image_format == "PNG":
            assert actual.text["Description"] == "Keep this text"
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert ImageOrientationReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
@pytest.mark.parametrize("orientation", [None, 1])
def test_no_change_returns_exact_original(image_format, orientation, tmp_path):
    artifact = Artifact(data=encoded(image_format, orientation))
    destination = tmp_path / "unused"
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=artifact, output_path=destination)
    )
    assert result.outcome == "no_change"
    assert result.artifact is artifact
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["L", "RGBA", "P"])
def test_png_mode_preserved(mode):
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=encoded(mode=mode), output_path=None)
    )
    assert result.outcome == "changed"
    with Image.open(BytesIO(result.artifact.read_bytes())) as image:
        assert image.mode == mode


@pytest.mark.parametrize("orientation", [0, 9, 65535])
@pytest.mark.parametrize("on_disk", [False, True])
def test_invalid_orientation(orientation, on_disk, tmp_path):
    destination = tmp_path / "output" if on_disk else None
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=encoded(orientation=orientation), output_path=destination)
    )
    assert result.outcome == "failed"
    assert result.report.issues[0].code == ImageOrientationFinding.INVALID_ORIENTATION
    assert result.artifact is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("data", [b"", b"not an image", encoded("JPEG")[:-50]])
def test_unreadable_data_is_not_success(data, tmp_path):
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=data, output_path=tmp_path / "output")
    )
    assert result.outcome == "failed"
    assert result.report.issues[0].code == ImageOrientationFinding.DECODE_FAILED
    assert result.artifact is None
    assert not list(tmp_path.iterdir())


def test_unsupported_format():
    with pixels() as image, BytesIO() as output:
        image.save(output, format="GIF")
        data = output.getvalue()
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=data, output_path=None)
    )
    assert result.report.issues[0].code == ImageOrientationFinding.UNSUPPORTED_FORMAT
    assert result.artifact is None


@pytest.mark.parametrize("image_format", ["TIFF", "PNG"])
def test_multiple_frames_never_dropped(image_format, tmp_path):
    with pixels() as image, BytesIO() as output:
        second = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        with second:
            image.save(output, format=image_format, save_all=True, append_images=[second])
        data = output.getvalue()
    result = PillowImageOrientationNormalizer().normalize(
        NormalizationRequest(source=data, output_path=tmp_path / "output")
    )
    assert result.outcome == "failed"
    assert result.report.issues[0].code == ImageOrientationFinding.MULTIPLE_FRAMES
    assert result.artifact is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error", [ValueError("encoder configuration"), OSError("disk full")])
def test_partial_output_cleanup(error, monkeypatch, tmp_path):
    def broken_save(self, output, **kwargs):
        output.write(b"partial")
        raise error

    data = encoded()
    monkeypatch.setattr(Image.Image, "save", broken_save)
    worker = PillowImageOrientationNormalizer()
    request = NormalizationRequest(source=data, output_path=tmp_path / "output")
    if isinstance(error, OSError):
        with pytest.raises(OSError) as caught:
            worker.normalize(request)
        assert caught.value is error
    else:
        result = worker.normalize(request)
        assert result.report.issues[0].code == ImageOrientationFinding.ENCODE_FAILED
        assert result.artifact is None
    assert not list(tmp_path.iterdir())


def test_existing_destination_protected(tmp_path):
    destination = tmp_path / "existing"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        PillowImageOrientationNormalizer().normalize(
            NormalizationRequest(source=encoded(), output_path=destination)
        )
    assert destination.read_bytes() == b"keep"


def test_shared_request_and_worker_contract():
    worker = PillowImageOrientationNormalizer()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_pillow_image_orientation"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize(b"bytes are not a request")
    # Text-only settings are not interpreted by this worker.
    request = NormalizationRequest(
        source=encoded(), output_path=None, declared_encoding="ascii"
    )
    result = worker.normalize(request)
    assert type(result) is NormalizationResult
    assert isinstance(result.report, ImageOrientationReport)
    assert result.outcome == "changed"


def test_chaining_returns_original_when_already_normalized():
    worker = PillowImageOrientationNormalizer()
    first = worker.normalize(NormalizationRequest(source=encoded(), output_path=None))
    second = worker.normalize(NormalizationRequest(source=first.artifact, output_path=None))
    assert second.outcome == "no_change"
    assert second.artifact is first.artifact


def test_models_reject_inconsistent_reports():
    with pytest.raises(ValidationError):
        ImageOrientationReport(outcome="failed")
    with pytest.raises(ValidationError):
        ImageOrientationReport(outcome="no_change", image_format="PNG", input_dimensions=(3, 2), source_orientation=6)
    with pytest.raises(ValidationError):
        ImageOrientationIssue(code="normalization.image.decode_failed", message=" ")
    report = ImageOrientationReport(
        outcome="no_change", image_format="PNG", input_dimensions=(3, 2)
    )
    with pytest.raises(ValueError):
        NormalizationResult(report=report, artifact=None)
    with pytest.raises(ValidationError):
        report.outcome = "changed"
