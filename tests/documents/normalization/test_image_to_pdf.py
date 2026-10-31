"""Image conversion rules, PDF geometry, and the shared storage boundary."""

from io import BytesIO

import pytest
from PIL import Image, TiffImagePlugin
from pydantic import ValidationError
from pypdf import PdfReader

from raggae.documents.normalization import (
    Artifact,
    NormalizationWorker,
    ImageToPdfFinding,
    ImageToPdfIssue,
    ImageToPdfReport,
    NormalizationRequest,
    NormalizationResult,
    PillowImageToPdfConverter,
)


def encoded(image_format="PNG", mode="RGB", *, dpi=(150, 150)):
    with Image.new(mode, (72, 36)) as image, BytesIO() as output:
        options = {} if dpi is None else {"dpi": dpi}
        image.save(output, format=image_format, **options)
        return output.getvalue()


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
@pytest.mark.parametrize("source_kind", ["bytes", "path", "artifact"])
@pytest.mark.parametrize("disk", [False, True])
def test_one_image_one_page(image_format, source_kind, disk, tmp_path):
    original = encoded(image_format)
    source = original
    if source_kind == "path":
        source = tmp_path / "not-an-image.pdf"
        source.write_bytes(original)
    elif source_kind == "artifact":
        source = Artifact(data=original)
    destination = tmp_path / "result.pdf" if disk else None
    request = NormalizationRequest(source=source, output_path=destination)
    result = PillowImageToPdfConverter().normalize(request)
    assert type(result) is NormalizationResult
    assert result.outcome == "changed"
    assert result.report.image_format == image_format
    assert result.report.dimensions == (72, 36)
    assert not result.report.transparency_flattened
    assert result.report.issues == ()
    assert request.source.read_bytes() == original
    assert result.artifact.path == destination
    pdf = result.artifact.read_bytes()
    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf), strict=True)
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert float(page.mediabox.width) == pytest.approx(72 * 72 / result.report.resolution_dpi)
    assert float(page.mediabox.height) == pytest.approx(36 * 72 / result.report.resolution_dpi)
    assert len(page.images) == 1
    assert page.images[0].image.size == (72, 36)
    assert page.images[0].image.mode == "RGB"
    assert not page.extract_text()
    assert not list(tmp_path.glob(".raggae-*.part"))
    assert ImageToPdfReport.model_validate_json(result.report.model_dump_json()) == result.report


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_missing_dpi_uses_150(image_format):
    result = PillowImageToPdfConverter().normalize(
        NormalizationRequest(source=encoded(image_format, dpi=None), output_path=None)
    )
    assert result.report.resolution_dpi == 150


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF"])
def test_unequal_dpi_keeps_pixel_proportions(image_format):
    source = encoded(image_format, dpi=(300, 72))
    with Image.open(BytesIO(source)) as image:
        expected_dpi = image.info["dpi"][0]
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.report.resolution_dpi == pytest.approx(expected_dpi)
    page = PdfReader(BytesIO(result.artifact.read_bytes())).pages[0]
    assert float(page.mediabox.width) / float(page.mediabox.height) == pytest.approx(2)
    assert float(page.mediabox.width) == pytest.approx(72 * 72 / expected_dpi)


@pytest.mark.parametrize("dpi", [None, (), [], "300", True, False, 0, -1, float("nan"), float("inf"), (0, 300), ("invalid", 300), 1e-320, 10**400])
def test_invalid_dpi_fallback(dpi):
    with Image.new("RGB", (72, 36)) as image:
        image.info["dpi"] = dpi
        assert PillowImageToPdfConverter._resolution(image) == 150


@pytest.mark.parametrize("dpi", [72, 300.5, (96, 72), [150, 200], TiffImagePlugin.IFDRational(300)])
def test_valid_uniform_resolution(dpi):
    with Image.new("RGB", (72, 36)) as image:
        image.info["dpi"] = dpi
        expected = dpi[0] if isinstance(dpi, (tuple, list)) else dpi
        assert PillowImageToPdfConverter._resolution(image) == expected


@pytest.mark.parametrize("mode,image_format", [
    ("1", "PNG"), ("L", "PNG"), ("P", "PNG"), ("I;16", "PNG"),
    ("CMYK", "JPEG"), ("CMYK", "TIFF"), ("RGBA", "PNG"), ("LA", "PNG"),
])
def test_modes_converted_to_rgb(mode, image_format):
    result = PillowImageToPdfConverter().normalize(
        NormalizationRequest(source=encoded(image_format, mode), output_path=None)
    )
    assert result.outcome == "changed"
    page = PdfReader(BytesIO(result.artifact.read_bytes())).pages[0]
    assert page.images[0].image.mode == "RGB"
    assert result.report.transparency_flattened == ("A" in mode)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB", "L"])
def test_alpha_and_colour_key_transparency_on_white(mode):
    with Image.new(mode, (72, 36)) as image, BytesIO() as output:
        options = {}
        if mode == "P":
            image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
            options["transparency"] = 0
        elif mode == "RGB":
            options["transparency"] = (0, 0, 0)
        elif mode == "L":
            options["transparency"] = 0
        image.save(output, format="PNG", **options)
        source = output.getvalue()
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.outcome == "changed"
    assert result.report.transparency_flattened
    extracted = PdfReader(BytesIO(result.artifact.read_bytes())).pages[0].images[0].image
    assert all(channel >= 250 for channel in extracted.getpixel((36, 18)))


def test_half_alpha_compositing():
    with Image.new("RGBA", (1, 1), (255, 0, 0, 128)) as image:
        with PillowImageToPdfConverter._to_rgb(image, True) as rgb:
            assert rgb.getpixel((0, 0)) == (255, 127, 127)


@pytest.mark.parametrize("image_format", ["PNG", "TIFF"])
def test_multiple_frames_rejected_without_output(image_format, tmp_path):
    with Image.new("RGB", (72, 36), "red") as first, Image.new("RGB", (72, 36), "blue") as second, BytesIO() as output:
        first.save(output, format=image_format, save_all=True, append_images=[second])
        source = output.getvalue()
    request = NormalizationRequest(source=source, output_path=tmp_path / "result.pdf")
    result = PillowImageToPdfConverter().normalize(request)
    assert result.artifact is None
    assert result.report.issues[0].code == ImageToPdfFinding.MULTIPLE_FRAMES
    assert request.source.read_bytes() == source
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("image_format", ["GIF", "BMP", "WEBP"])
def test_unsupported_formats(image_format):
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=encoded(image_format, dpi=None), output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageToPdfFinding.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("source", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_malformed_images_fail_safely(source, tmp_path):
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=tmp_path / "result.pdf"))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageToPdfFinding.DECODE_FAILED
    assert not list(tmp_path.iterdir())


def test_broken_pixel_data_loaded_before_success(monkeypatch):
    source = encoded()

    def broken_load(self, *args, **kwargs):
        raise OSError("private decoding diagnostic")

    from PIL import PngImagePlugin
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", broken_load)
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=None))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageToPdfFinding.DECODE_FAILED
    assert "private" not in result.report.model_dump_json()


@pytest.mark.parametrize("error", [OSError("private"), ValueError("private"), TypeError("private"), KeyError("private")])
def test_encoder_failure_safe_and_scratch_removed(error, monkeypatch, tmp_path):
    source = encoded()

    def broken_save(self, output, **kwargs):
        output.write(b"partial PDF")
        raise error

    monkeypatch.setattr(Image.Image, "save", broken_save)
    result = PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=tmp_path / "result.pdf"))
    assert result.artifact is None
    assert result.report.issues[0].code == ImageToPdfFinding.ENCODE_FAILED
    assert "private" not in result.report.model_dump_json()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("write_result", [None, 0, -1, "error"])
def test_destination_failure_is_not_encoder_failure(write_result):
    class BrokenOutput:
        def write(self, data):
            if write_result == "error":
                raise OSError("destination error")
            return write_result

    with BytesIO(encoded()) as source, pytest.raises(OSError):
        PillowImageToPdfConverter()._transform(source, BrokenOutput())


def test_partial_destination_writes_complete_pdf():
    class PartialOutput(BytesIO):
        def write(self, data):
            return super().write(data[:17])

    with BytesIO(encoded()) as source, PartialOutput() as output:
        report = PillowImageToPdfConverter()._transform(source, output)
        assert report.outcome == "changed"
        assert len(PdfReader(BytesIO(output.getvalue())).pages) == 1


def test_publish_error_cleans_scratch(monkeypatch, tmp_path):
    from raggae.documents.normalization import io

    def broken_link(*args):
        raise OSError("destination error")

    monkeypatch.setattr(io.os, "link", broken_link)
    with pytest.raises(OSError, match="destination error"):
        PillowImageToPdfConverter().normalize(NormalizationRequest(source=encoded(), output_path=tmp_path / "result.pdf"))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("same_source", [False, True])
def test_existing_destination_and_original_preserved(same_source, tmp_path):
    source = tmp_path / "original.png"
    original = encoded()
    source.write_bytes(original)
    destination = source if same_source else tmp_path / "result.pdf"
    if not same_source:
        destination.write_bytes(b"existing file")
    with pytest.raises(FileExistsError):
        PillowImageToPdfConverter().normalize(NormalizationRequest(source=source, output_path=destination))
    assert source.read_bytes() == original
    assert destination.read_bytes() == (original if same_source else b"existing file")
    assert not list(tmp_path.glob(".raggae-*.part"))


def test_contract_and_unused_settings():
    worker = PillowImageToPdfConverter()
    assert isinstance(worker, NormalizationWorker)
    assert worker.name == "normalization_pillow_image_to_pdf"
    assert not hasattr(worker, "implementation")
    assert not hasattr(worker, "version")
    with pytest.raises(TypeError, match="NormalizationRequest"):
        worker.normalize({})
    result = worker.normalize(NormalizationRequest(source=Artifact(data=encoded(), password="unused-secret"), output_path=None,  declared_encoding="ascii"))
    assert result.outcome == "changed"
    assert "unused-secret" not in repr(result)


def test_report_immutable_serializable():
    report = ImageToPdfReport(outcome="changed", image_format="PNG", dimensions=(72, 36), resolution_dpi=150)
    assert ImageToPdfReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.resolution_dpi = 72
    with pytest.raises(ValidationError):
        ImageToPdfIssue(code=ImageToPdfFinding.DECODE_FAILED, message=" ")


@pytest.mark.parametrize("kwargs", [
    {"outcome": "failed"}, {"outcome": "no_change"}, {"outcome": "changed"},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (0, 2), "resolution_dpi": 150},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (1, 2), "resolution_dpi": float("inf")},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (1, 2), "resolution_dpi": 0},
    {"outcome": "failed", "transparency_flattened": True, "issues": [{"code": ImageToPdfFinding.DECODE_FAILED, "message": "Failed"}]},
    {"outcome": "failed", "image_format": "PNG", "issues": [{"code": ImageToPdfFinding.DECODE_FAILED, "message": "Failed"}]},
    {"outcome": "changed", "image_format": "PNG", "dimensions": (1, 2), "resolution_dpi": 150, "issues": [{"code": ImageToPdfFinding.DECODE_FAILED, "message": "Failed"}]},
])
def test_inconsistent_reports_rejected(kwargs):
    with pytest.raises(ValidationError):
        ImageToPdfReport(**kwargs)
