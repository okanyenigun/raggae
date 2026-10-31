"""Service-independent file handling and adapter boundary checks."""

from raggae.documents.layout_analysis import VisualLayoutPolicy

from raggae.documents.normalization import Artifact

from io import BytesIO
from types import SimpleNamespace
import subprocess
import sys

from PIL import Image, ImageChops
import pytest
from pydantic import ValidationError

from raggae.documents.layout_analysis import (
    DoclingLayoutService, GeometricReadingOrderResolver, ImageFrameLocation,
    LayoutAnalysisResult, LayoutAnalyzer, LayoutDetection,
    LayoutDetectionService, LayoutRegion, LayoutSurface, PaddleLayoutService,
    PdfPageLocation, VisualLayoutAnalyzer,
)
from raggae.documents.layout_analysis.region_discovery.visual.services.labels import region_kind


class Detector:
    def __init__(self, detections=None):
        self.detections = detections
        self.calls = []

    def detect(self, image):
        self.calls.append((image.mode, image.size, image.getpixel((0, 0))))
        if self.detections is not None:
            return self.detections
        return (LayoutDetection(kind="text", bbox=(0, 0, image.width, image.height)),)


def raster(format="PNG", *, size=(80, 60), mode="RGB", color="white", frames=1, orientation=None):
    with BytesIO() as output, Image.new(mode, size, color) as image:
        options = {}
        if frames > 1:
            options.update(save_all=True, append_images=[image] * (frames - 1))
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        image.save(output, format=format, **options)
        return output.getvalue()


def codes(result):
    return {f.code.removeprefix("layout.visual.") for f in result.findings}


def test_public_protocols_and_positional_sources(tmp_path):
    detector = Detector()
    worker = VisualLayoutAnalyzer(service=detector)
    assert isinstance(detector, LayoutDetectionService)
    assert isinstance(worker, LayoutAnalyzer)
    assert worker.name == "layout_analysis_visual"
    data = raster()
    path = tmp_path / "photo.png"
    path.write_bytes(data)
    expected = worker.analyze(source=data)
    assert expected == worker.analyze(path)
    assert path.read_bytes() == data
    assert expected.outcome == "success"
    assert expected.surfaces[0].unit == "px"
    assert expected.regions[0].native_order is None
    assert detector.calls == [("RGB", (80, 60), (255, 255, 255))] * 2
    with pytest.raises(TypeError):
        worker.analyze(path, source=data)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "TIFF", "WEBP", "BMP", "GIF"])
def test_supported_raster_formats(format):
    result = VisualLayoutAnalyzer(service=Detector()).analyze(source=raster(format))
    assert result.outcome == "success"
    assert result.regions[0].bbox == (0, 0, 80, 60)


def test_transparency_is_composited_on_white():
    detector = Detector()
    VisualLayoutAnalyzer(service=detector).analyze(source=raster(mode="RGBA", color=(0, 0, 0, 0)))
    assert detector.calls[0][2] == (255, 255, 255)


def test_tiff_frames_and_limit():
    worker = VisualLayoutAnalyzer(service=Detector(), policy=VisualLayoutPolicy(max_pages=2))
    full = worker.analyze(source=raster("TIFF", frames=2))
    assert full.outcome == "success"
    assert [s.location.frame_number for s in full.surfaces] == [1, 2]
    limited = worker.analyze(source=raster("TIFF", frames=3))
    assert limited.outcome == "partial" and len(limited.surfaces) == 2
    assert codes(limited) == {"page_limit"}


def test_exif_is_not_silently_applied():
    result = VisualLayoutAnalyzer(service=Detector()).analyze(source=raster("JPEG", orientation=6))
    assert result.outcome == "partial"
    assert (result.surfaces[0].width, result.surfaces[0].height) == (80, 60)
    assert codes(result) == {"stored_orientation"}


def test_tiff_uses_the_decoders_oriented_pixel_coordinates():
    data = raster("TIFF", orientation=6)
    result = VisualLayoutAnalyzer(service=Detector()).analyze(source=data)
    with Image.open(BytesIO(data)) as reopened:
        reopened.load()
        assert (result.surfaces[0].width, result.surfaces[0].height) == reopened.size
        assert result.regions[0].bbox == (0, 0, *reopened.size)
    assert result.outcome == "partial" and codes(result) == {"decoded_orientation"}


@pytest.mark.parametrize("option", ["dpi", "max_source_bytes", "max_pages", "max_pixels_per_page", "max_regions_per_page"])
@pytest.mark.parametrize("value,error", [(0, ValidationError), (-1, ValidationError), (True, ValidationError), (1.2, ValidationError)])
def test_constructor_limits(option, value, error):
    with pytest.raises(error):
        VisualLayoutAnalyzer(service=Detector(), policy=VisualLayoutPolicy(**{option: value}))


def test_requires_a_service():
    with pytest.raises(TypeError):
        VisualLayoutAnalyzer(service=object())


@pytest.mark.parametrize("bbox", [(0, 0, 0, 5), (4, 0, 3, 5), (0, 5, 2, 4), (0, 0, float("nan"), 5), (0, 0, float("inf"), 5)])
def test_invalid_detection_boxes(bbox):
    with pytest.raises(ValidationError):
        LayoutDetection(kind="text", bbox=bbox)


def test_clipping_unknown_and_region_limit():
    detections = (
        LayoutDetection(kind="text", bbox=(-10, -5, 90, 70)),
        LayoutDetection(kind="unknown", bbox=(2, 2, 10, 10)),
        LayoutDetection(kind="table", bbox=(3, 3, 10, 10)),
    )
    result = VisualLayoutAnalyzer(service=Detector(detections), policy=VisualLayoutPolicy(max_regions_per_page=2)).analyze(source=raster())
    assert result.outcome == "partial" and len(result.regions) == 2
    assert result.regions[0].bbox == (0, 0, 80, 60)
    assert codes(result) == {"box_clipped", "unknown_label", "region_limit"}


def test_outside_and_empty_predictions_do_not_claim_a_blank_page():
    for detections in [(), (LayoutDetection(kind="text", bbox=(90, 0, 100, 10)),)]:
        result = VisualLayoutAnalyzer(service=Detector(detections)).analyze(source=raster())
        assert result.outcome == "partial" and not result.regions
        assert "no_regions" in codes(result)


def test_service_failure_does_not_leak_diagnostics_or_keep_half_page():
    class Broken:
        def detect(self, image):
            yield LayoutDetection(kind="text", bbox=(0, 0, 10, 10))
            raise RuntimeError("sensitive-file-secret")
    result = VisualLayoutAnalyzer(service=Broken()).analyze(source=raster())
    assert result.outcome == "partial" and not result.regions
    assert codes(result) == {"inference_failed"}
    assert "sensitive" not in result.model_dump_json()


def test_limits_prevent_inference(tmp_path):
    detector = Detector()
    data = raster()
    path = tmp_path / "source.png"
    path.write_bytes(data)
    for source in [data, path]:
        result = VisualLayoutAnalyzer(service=detector, policy=VisualLayoutPolicy(max_source_bytes=len(data) - 1)).analyze(source=source)
        assert result.outcome == "failed" and codes(result) == {"source_too_large"}
    result = VisualLayoutAnalyzer(service=detector, policy=VisualLayoutPolicy(max_pixels_per_page=4799)).analyze(source=data)
    assert result.outcome == "failed" and codes(result) == {"pixel_limit"}
    assert not detector.calls


def test_missing_directory_empty_and_unsupported_sources(tmp_path):
    worker = VisualLayoutAnalyzer(service=Detector())
    for source in [tmp_path / "missing", tmp_path]:
        assert codes(worker.analyze(source=source)) == {"source_unreadable"}
    assert worker.analyze(source=b"").outcome == "failed"
    assert worker.analyze(source=b"<html>not an image</html>").outcome == "not_applicable"
    assert worker.analyze(source=b"%PDF-broken").outcome == "failed"
    assert worker.analyze(source=raster("PNG")[:40]).outcome in {"not_applicable", "failed"}


@pytest.mark.parametrize("kind,location,unit", [
    ("image", ImageFrameLocation(frame_number=1), "pt"),
    ("image", PdfPageLocation(page_number=1), "px"),
    ("page", PdfPageLocation(page_number=1), "px"),
])
def test_surface_rejects_mismatched_units_and_locators(kind, location, unit):
    with pytest.raises(ValidationError):
        LayoutSurface(id_="s", kind=kind, location=location, width=10, height=10, unit=unit)


def test_image_boxes_must_fit_and_can_feed_geometry_resolver():
    surface = LayoutSurface(id_="s", kind="image", location=ImageFrameLocation(frame_number=1), width=100, height=100)
    with pytest.raises(ValidationError):
        LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(LayoutRegion(id="r", surface_id="s", kind="text", bbox=(0, 0, 101, 20)),))
    layout = LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(
        LayoutRegion(id="a", surface_id="s", kind="text", bbox=(10, 10, 80, 30)),
        LayoutRegion(id="b", surface_id="s", kind="text", bbox=(10, 50, 80, 70)),
    ))
    order = GeometricReadingOrderResolver().resolve_order(layout)
    assert order.outcome == "success" and len(order.edges) == 1


def test_pdf_pages_password_limits_and_points(make_pdf):
    pytest.importorskip("pypdfium2")
    data = make_pdf((b"", b""), password="fixture-secret")
    detector = Detector()
    worker = VisualLayoutAnalyzer(service=detector, policy=VisualLayoutPolicy(max_pages=1))
    assert codes(worker.analyze(source=data)) == {"password_required"}
    assert codes(worker.analyze(source=Artifact(data=data, password="wrong"))) == {"invalid_password"}
    result = worker.analyze(source=Artifact(data=data, password="fixture-secret"))
    assert result.outcome == "partial" and codes(result) == {"page_limit"}
    assert result.regions[0].bbox == (0, 0, 300, 400)
    assert detector.calls == [("RGB", (600, 800), (255, 255, 255))]
    assert result.surfaces[0].unit == "pt"


@pytest.mark.parametrize("rotation,expected", [
    (0, (20, 40, 60, 80)), (90, (240, 20, 280, 60)),
    (180, (190, 240, 230, 280)), (270, (40, 190, 80, 230)),
])
@pytest.mark.parametrize("unit", [1, 2])
def test_actual_render_crop_rotation_origin_userunit(make_pdf, rotation, expected, unit):
    pytest.importorskip("pypdfium2")
    class RedRegion:
        def detect(self, image):
            red, green, blue = image.split()
            with red, green, blue, ImageChops.subtract(red, green) as difference:
                with difference.point(lambda value: 255 if value > 150 else 0) as mask:
                    return (LayoutDetection(kind="image", bbox=mask.getbbox()),)
    data = make_pdf((b"1 0 0 rg 50 300 40 40 re f",), media=(10, 20, 310, 420), crop=(30, 60, 280, 380), rotation=rotation, unit=unit)
    result = VisualLayoutAnalyzer(service=RedRegion()).analyze(source=data)
    assert result.outcome == "success"
    assert result.regions[0].bbox == pytest.approx(tuple(v * unit for v in expected), abs=0.6)
    dimensions = (250, 320) if rotation in (0, 180) else (320, 250)
    assert (result.surfaces[0].width, result.surfaces[0].height) == tuple(v * unit for v in dimensions)


def test_pdf_pixel_limit_and_annotation_coverage(make_pdf):
    pytest.importorskip("pypdfium2")
    detector = Detector()
    result = VisualLayoutAnalyzer(service=detector, policy=VisualLayoutPolicy(max_pixels_per_page=10)).analyze(source=make_pdf())
    assert result.outcome == "failed" and codes(result) == {"pixel_limit"}
    assert not detector.calls
    result = VisualLayoutAnalyzer(service=detector).analyze(source=make_pdf(annotations=True))
    assert result.outcome == "partial" and "annotation_coverage" in codes(result)


@pytest.mark.parametrize("label,kind", [("section_header", "heading"), ("doc_title", "heading"), ("figure_caption", "caption"), ("picture", "image"), ("table", "table"), ("list_item", "text"), ("formula", "unknown"), ("novel-label", "unknown")])
def test_label_mapping(label, kind):
    assert region_kind(label) == kind


@pytest.mark.parametrize("service", [PaddleLayoutService, DoclingLayoutService])
def test_service_requires_existing_local_directory_before_import(service, tmp_path):
    with pytest.raises(FileNotFoundError):
        service(model_path=tmp_path / "missing")
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError):
        service(model_path=file)
    with pytest.raises(ValueError):
        service(model_path=tmp_path, threshold=float("nan"))


def test_package_import_does_not_load_models_or_renderer():
    code = '''
import sys
from raggae.documents.layout_analysis import VisualLayoutAnalyzer, PaddleLayoutService, DoclingLayoutService
assert not any(name in sys.modules for name in ('paddleocr','paddle','torch','transformers','pypdfium2'))
'''
    subprocess.run([sys.executable, "-c", code], check=True)


def test_paddle_adapter_bgr_input_and_result(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    from raggae.documents.layout_analysis.region_discovery.visual.services import paddle
    seen = {}
    class Backend:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def predict(self, *, input, batch_size):
            assert input[0, 0].tolist() == [30, 20, 10]
            assert batch_size == 1
            yield {"boxes": [{"label": "table", "coordinate": [1, 2, 5, 8]}]}
    monkeypatch.setattr(paddle, "import_module", lambda name: SimpleNamespace(LayoutDetection=Backend))
    service = PaddleLayoutService(model_path=tmp_path)
    with Image.new("RGB", (10, 10), (10, 20, 30)) as image:
        result = service.detect(image)
    assert result == (LayoutDetection(kind="table", bbox=(1, 2, 5, 8)),)
    assert seen["model_dir"] == str(tmp_path.resolve()) and seen["device"] == "cpu"


def test_docling_adapter_local_loading_and_pixel_scaling(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from raggae.documents.layout_analysis.region_discovery.visual.services import docling
    seen = {}
    class Tensor:
        def item(self): return 4
        def tolist(self): return [1, 2, 5, 8]
    class Inputs(dict):
        def to(self, device):
            seen["device"] = device
            return self
    class Processor:
        def __call__(self, **kwargs): return Inputs()
        def post_process_object_detection(self, outputs, **kwargs):
            seen.update(kwargs)
            return [{"labels": [Tensor()], "boxes": [Tensor()]}]
    class Model:
        config = SimpleNamespace(id2label={4: "section_header"})
        def to(self, device): return self
        def eval(self): return self
        def __call__(self, **kwargs): return object()
    def loader(value):
        def load(path, **kwargs):
            assert path == str(tmp_path.resolve())
            assert kwargs == {"local_files_only": True, "trust_remote_code": False}
            return value
        return SimpleNamespace(from_pretrained=load)
    backend = SimpleNamespace(AutoImageProcessor=loader(Processor()), AutoModelForObjectDetection=loader(Model()))
    monkeypatch.setattr(docling, "import_module", lambda name: backend if name == "transformers" else SimpleNamespace(inference_mode=nullcontext))
    service = DoclingLayoutService(model_path=tmp_path)
    with Image.new("RGB", (40, 20)) as image:
        detections = service.detect(image)
    assert detections == (LayoutDetection(kind="heading", bbox=(1, 2, 5, 8)),)
    assert seen == {"device": "cpu", "threshold": 0.6, "target_sizes": [(20, 40)]}
