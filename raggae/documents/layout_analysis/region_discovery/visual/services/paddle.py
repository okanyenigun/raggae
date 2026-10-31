"""PaddleOCR's standalone layout detector; no OCR or document pipeline."""

from importlib import import_module
from pathlib import Path
from PIL import Image
from .labels import region_kind
from ..base import LayoutDetection


class PaddleLayoutService:
    def __init__(
        self,
        *,
        model_path: str | Path,
        model_name: str = "PP-DocLayout-S",
        device: str = "cpu",
        threshold: float = 0.5,
    ) -> None:
        path = Path(model_path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("model_path must be a local model directory")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between zero and one")
        try:
            paddleocr = import_module("paddleocr")
        except ImportError as error:
            raise ImportError(
                "Paddle layout requires: pip install 'raggae[layout-paddle]'"
            ) from error
        self._model = paddleocr.LayoutDetection(
            model_name=model_name,
            model_dir=str(path),
            device=device,
            threshold=threshold,
        )

    def detect(self, image: Image.Image) -> tuple[LayoutDetection, ...]:
        import numpy as np

        # PaddleX ndarray inputs follow OpenCV's BGR convention.
        pixels = np.asarray(image)[:, :, ::-1].copy()
        results = list(self._model.predict(input=pixels, batch_size=1))
        if len(results) != 1:
            raise ValueError("expected one Paddle result for one image")
        return tuple(
            LayoutDetection(
                kind=region_kind(box["label"]), bbox=tuple(box["coordinate"])
            )
            for box in results[0]["boxes"]
        )
