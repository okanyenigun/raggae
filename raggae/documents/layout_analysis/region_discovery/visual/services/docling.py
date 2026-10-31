"""Docling's Heron-family layout model, run locally through Transformers."""

from importlib import import_module
from pathlib import Path
from PIL import Image
from .labels import region_kind
from ..base import LayoutDetection


class DoclingLayoutService:
    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "cpu",
        threshold: float = 0.6,
    ) -> None:
        path = Path(model_path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("model_path must be a local model directory")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between zero and one")
        try:
            transformers = import_module("transformers")
        except ImportError as error:
            raise ImportError(
                "Docling layout requires: pip install 'raggae[layout-docling]'"
            ) from error
        options = {"local_files_only": True, "trust_remote_code": False}
        self._processor = transformers.AutoImageProcessor.from_pretrained(
            str(path), **options
        )
        self._model = transformers.AutoModelForObjectDetection.from_pretrained(
            str(path), **options
        )
        self._model.to(device).eval()
        self._device = device
        self._threshold = threshold

    def detect(self, image: Image.Image) -> tuple[LayoutDetection, ...]:
        torch = import_module("torch")

        inputs = self._processor(images=[image], return_tensors="pt").to(self._device)
        with torch.inference_mode():
            outputs = self._model(**inputs)
        results = self._processor.post_process_object_detection(
            outputs,
            target_sizes=[(image.height, image.width)],
            threshold=self._threshold,
        )
        if len(results) != 1:
            raise ValueError("expected one Docling result for one image")
        result = results[0]
        return tuple(
            LayoutDetection(
                kind=region_kind(self._model.config.id2label[int(label.item())]),
                bbox=tuple(box.tolist()),
            )
            for label, box in zip(result["labels"], result["boxes"], strict=True)
        )
