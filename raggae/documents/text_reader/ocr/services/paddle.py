"""Local PaddleOCR text detection + recognition from explicit model folders."""

from pathlib import Path
from typing import Any
from PIL import Image


class PaddleOcrService:
    """Recognize text with PaddleOCR's detection and recognition models.

    Both models are loaded from local folders (Paddle inference models such as
    PaddlePaddle's ``PP-OCRv5_mobile_det`` and ``en_PP-OCRv5_mobile_rec``); the
    recognition model decides the language. Nothing is downloaded. Page
    orientation, unwarping, and text-line orientation are off: the reader
    supplies pages as displayed. Models load at construction.
    """

    def __init__(
        self,
        *,
        detection_model_dir: Path | str,
        recognition_model_dir: Path | str,
        detection_model_name: str = "PP-OCRv5_mobile_det",
        recognition_model_name: str = "en_PP-OCRv5_mobile_rec",
        device: str = "cpu",
    ) -> None:
        detection = PaddleOcrService._model_folder(
            detection_model_dir, "detection_model_dir"
        )
        recognition = PaddleOcrService._model_folder(
            recognition_model_dir, "recognition_model_dir"
        )
        for value, label in (
            (detection_model_name, "detection_model_name"),
            (recognition_model_name, "recognition_model_name"),
            (device, "device"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        try:
            import paddleocr
        except ImportError as error:
            raise ImportError(
                "PaddleOCR requires: pip install 'raggae[text-paddle]'"
            ) from error
        try:
            self._pipeline: Any = paddleocr.PaddleOCR(
                text_detection_model_name=detection_model_name,
                text_detection_model_dir=str(detection),
                text_recognition_model_name=recognition_model_name,
                text_recognition_model_dir=str(recognition),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device=device,
            )
        except (RuntimeError, ValueError, KeyError, OSError, AssertionError) as error:
            raise ValueError(
                "The model folders or names are not a usable PaddleOCR detection "
                "and recognition pair."
            ) from error
        self._recognition_model_name = recognition_model_name

    @property
    def recognition_model_name(self) -> str:
        return self._recognition_model_name

    def recognize(self, image: Image.Image) -> str:
        import numpy as np

        # PaddleX ndarray inputs follow OpenCV's BGR convention.
        pixels = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        results = list(self._pipeline.predict(pixels))
        if len(results) != 1:
            raise ValueError("expected one PaddleOCR result for one image")
        result = results[0]
        return PaddleOcrService._lines(
            list(result["rec_texts"]), [tuple(box) for box in result["rec_boxes"]]
        )

    @staticmethod
    def _lines(texts: list[Any], boxes: list[tuple[float, ...]]) -> str:
        """Rebuild lines: PaddleOCR often returns several boxes (even words) per line.

        Boxes whose vertical extents overlap by at least half of the shorter box's
        height share a line; a line's boxes are joined left to right with a space,
        and lines are joined top to bottom with a newline.
        """
        items = sorted(
            (
                (
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                    text.strip(),
                )
                for text, box in zip(texts, boxes)
                if isinstance(text, str) and text.strip()
            ),
            key=lambda item: (item[1], item[0]),
        )
        lines: list[list[tuple[float, float, float, float, str]]] = []
        bands: list[tuple[float, float]] = []
        for item in items:
            top, bottom = item[1], item[3]
            for number, (band_top, band_bottom) in enumerate(bands):
                overlap = min(bottom, band_bottom) - max(top, band_top)
                if overlap >= 0.5 * min(bottom - top, band_bottom - band_top):
                    lines[number].append(item)
                    bands[number] = (min(top, band_top), max(bottom, band_bottom))
                    break
            else:
                lines.append([item])
                bands.append((top, bottom))
        ordered = sorted(zip(bands, lines), key=lambda pair: pair[0][0])
        return "\n".join(
            " ".join(item[4] for item in sorted(line, key=lambda item: item[0]))
            for _, line in ordered
        )

    @staticmethod
    def _model_folder(value: Path | str, label: str) -> Path:
        if not isinstance(value, (Path, str)):
            raise TypeError(f"{label} must be a path to a local PaddleOCR model folder")
        path = Path(value).expanduser()
        if not path.is_dir() or not (path / "inference.yml").is_file():
            raise ValueError(
                f"{label} must be a local PaddleOCR model folder (with inference.yml)"
            )
        return path.resolve()
