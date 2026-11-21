"""PaddleOcrService: local PP-OCR detection + recognition, never downloading."""

import builtins
import os
import socket
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.text_reader import OcrService, OcrTextReader, PaddleOcrService

MODELS = Path(
    os.environ.get(
        "RAGGAE_PADDLEOCR_MODELS", Path.home() / ".cache" / "raggae" / "paddleocr"
    )
)
DETECTION = MODELS / "PP-OCRv5_mobile_det"
RECOGNITION = MODELS / "en_PP-OCRv5_mobile_rec"

needs_models = pytest.mark.skipif(
    not (
        (DETECTION / "inference.yml").is_file()
        and (RECOGNITION / "inference.yml").is_file()
    ),
    reason="PaddleOCR models not downloaded (set RAGGAE_PADDLEOCR_MODELS)",
)


def picture(lines, size=(1000, 300)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(lines):
        draw.text(
            (40, 40 + number * 100),
            line,
            fill="black",
            font=ImageFont.load_default(size=44),
        )
    return image


def fake_model(folder):
    folder.mkdir(parents=True)
    (folder / "inference.yml").write_text("Global: {}\n")
    return folder


# Configuration ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"detection_model_dir": None}, TypeError),
        ({"detection_model_dir": "/no/such/folder"}, ValueError),
        ({"recognition_model_dir": 3}, TypeError),
        ({"detection_model_name": ""}, ValueError),
        ({"recognition_model_name": None}, ValueError),
        ({"device": " "}, ValueError),
    ],
)
def test_configuration_is_validated(tmp_path, kwargs, error):
    base = {
        "detection_model_dir": fake_model(tmp_path / "det"),
        "recognition_model_dir": fake_model(tmp_path / "rec"),
    }
    with pytest.raises(error):
        PaddleOcrService(**{**base, **kwargs})


def test_a_folder_without_inference_yml_is_not_a_model(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="inference.yml"):
        PaddleOcrService(detection_model_dir=empty, recognition_model_dir=empty)


def test_model_folders_are_required():
    with pytest.raises(TypeError):
        PaddleOcrService()  # type: ignore[call-arg]


def test_unusable_model_folders_are_a_configuration_error(tmp_path):
    pytest.importorskip("paddleocr", reason="Install raggae[text-paddle]")
    with pytest.raises(ValueError, match="not a usable PaddleOCR"):
        PaddleOcrService(
            detection_model_dir=fake_model(tmp_path / "det"),
            recognition_model_dir=fake_model(tmp_path / "rec"),
        )


def test_missing_backend_has_an_install_hint(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def without_paddle(name, *args, **kwargs):
        if name == "paddleocr" or name.startswith("paddleocr."):
            raise ImportError("no paddleocr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_paddle)
    with pytest.raises(ImportError, match=r"raggae\[text-paddle\]"):
        PaddleOcrService(
            detection_model_dir=fake_model(tmp_path / "det"),
            recognition_model_dir=fake_model(tmp_path / "rec"),
        )


# Line assembly (no model needed) -----------------------------------------------


@pytest.mark.parametrize(
    "texts,boxes,expected",
    [
        (
            ["Purchase", "report"],
            [(1, 1, 244, 51), (259, 4, 414, 54)],
            "Purchase report",
        ),
        (
            ["Shipping", "labels", "Cost: 90", "USD"],
            [
                (2, 0, 199, 52),
                (204, 1, 335, 44),
                (2, 53, 194, 103),
                (197, 56, 304, 101),
            ],
            "Shipping labels\nCost: 90 USD",
        ),
        # Boxes arrive out of order and a line is slightly skewed.
        (
            ["world", "Hello", "Second"],
            [(120, 8, 200, 40), (0, 0, 100, 34), (0, 60, 150, 95)],
            "Hello world\nSecond",
        ),
        # Two columns side by side share each band and are read across.
        (
            ["Left one", "Right one", "Left two", "Right two"],
            [(0, 0, 100, 30), (300, 2, 420, 31), (0, 40, 100, 70), (300, 41, 420, 71)],
            "Left one Right one\nLeft two Right two",
        ),
        # Empty recognitions are dropped; nothing recognized gives "".
        (
            ["", "  ", "Kept"],
            [(0, 0, 10, 10), (0, 20, 10, 30), (0, 40, 50, 60)],
            "Kept",
        ),
        ([], [], ""),
    ],
)
def test_boxes_are_rebuilt_into_lines(texts, boxes, expected):
    assert PaddleOcrService._lines(texts, boxes) == expected


# Real engine ----------------------------------------------------------------


@pytest.fixture(scope="module")
def paddle():
    pytest.importorskip("paddleocr", reason="Install raggae[text-paddle]")
    if not (DETECTION.is_dir() and RECOGNITION.is_dir()):
        pytest.skip("PaddleOCR models not downloaded")
    return PaddleOcrService(
        detection_model_dir=DETECTION, recognition_model_dir=RECOGNITION
    )


@needs_models
def test_recognizes_lines_offline_and_blank_images_are_empty(paddle, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert isinstance(paddle, OcrService)
    assert paddle.recognition_model_name == "en_PP-OCRv5_mobile_rec"
    assert paddle.recognize(
        picture(["Delivery note 2026-118", "Shipping labels: 600"])
    ) == ("Delivery note 2026-118\nShipping labels: 600")
    assert paddle.recognize(Image.new("RGB", (200, 100), "white")) == ""
    assert paddle.recognize(picture(["Grey scale"]).convert("L")) == "Grey scale"


@needs_models
def test_works_through_ocr_text_reader_for_images_and_pdf_regions(
    paddle, make_reader_pdf
):
    reader = OcrTextReader(service=paddle)
    buffer = BytesIO()
    picture(["Scanned receipt 42"]).save(buffer, "PNG")
    assert [b.text for b in reader.read(buffer.getvalue()).blocks] == [
        "Scanned receipt 42"
    ]
    data = make_reader_pdf(
        (
            b"BT /F1 14 Tf 30 380 Td (Purchase report) Tj ET\n"
            b"BT /F1 12 Tf 30 330 Td (Shipping labels) Tj 0 -14 Td (Cost: 90 USD) Tj ET",
        )
    )
    layout = NativePdfLayoutAnalyzer().analyze(data)
    result = reader.read(data, layout=layout)
    assert result.outcome == "success"
    assert [b.text for b in result.blocks] == [
        "Purchase report",
        "Shipping labels\nCost: 90 USD",
    ]
    assert all(b.method == "ocr" for b in result.blocks)
