"""Transcription through a vision model served by a local Ollama server."""

import base64
import ipaddress
import json
from io import BytesIO
from math import isfinite
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from PIL import Image
from .....prompts import PromptHub


class OllamaOcrService:
    """Ask a vision-capable model on a local Ollama server to transcribe images.

    ``model`` is required and must already be installed in Ollama with the
    ``vision`` capability; it is never pulled. The endpoint defaults to
    ``http://127.0.0.1:11434``; a non-loopback endpoint is refused unless
    ``allow_remote=True``, because page images would leave this machine.
    Proxy settings from the environment are ignored. ``prompt`` is the
    instruction sent with each image; by default it is the prompt hub's
    ``ocr.transcription`` version ``v1``, pinned so a newer version never
    changes the output silently. Requests use temperature 0 and a fixed seed;
    a response cut
    short by the model raises instead of returning partial text. ``timeout``
    (seconds) limits each request; ``None`` imposes no limit.
    """

    def __init__(
        self,
        *,
        model: str,
        endpoint: str = "http://127.0.0.1:11434",
        allow_remote: bool = False,
        timeout: float | None = None,
        prompt: str | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must name an installed Ollama vision model")
        if prompt is None:
            prompt = PromptHub().get("ocr.transcription", "v1").text
        elif not isinstance(prompt, str):
            raise TypeError("prompt must be a string or None")
        elif not prompt.strip():
            raise ValueError("prompt must not be blank")
        self._prompt = prompt
        if not isinstance(allow_remote, bool):
            raise TypeError("allow_remote must be a bool")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number of seconds or None")
        self._endpoint = self._checked_endpoint(endpoint, allow_remote=allow_remote)
        self._model = model
        self._timeout = timeout
        # Never route page images through an environment-configured proxy.
        self._opener = build_opener(ProxyHandler({}))
        try:
            details = self._post("/api/show", {"model": model})
        except HTTPError as error:
            if error.code == 404:
                raise ValueError(
                    f"Ollama model {model!r} is not installed; pull it yourself "
                    f"(ollama pull {model}). It is never pulled automatically."
                ) from error
            raise ConnectionError(
                "The Ollama server rejected the model check."
            ) from error
        if "vision" not in (details.get("capabilities") or []):
            raise ValueError(f"Ollama model {model!r} has no vision capability.")

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt(self) -> str:
        return self._prompt

    def recognize(self, image: Image.Image) -> str:
        with BytesIO() as buffer:
            image.convert("RGB").save(buffer, "PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        reply = self._post(
            "/api/generate",
            {
                "model": self._model,
                "prompt": self._prompt,
                "images": [encoded],
                "stream": False,
                "options": {"temperature": 0, "seed": 0},
            },
        )
        if reply.get("done") is not True or reply.get("done_reason") != "stop":
            raise RuntimeError("The model's transcription was cut short or incomplete.")
        text = reply.get("response")
        if not isinstance(text, str):
            raise RuntimeError("The Ollama server returned no transcription text.")
        return text.strip()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self._endpoint + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError:
            raise
        except (URLError, OSError) as error:
            raise ConnectionError(
                f"The Ollama server is not reachable at {self._endpoint}."
            ) from error
        except ValueError as error:
            raise RuntimeError("The Ollama server returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise RuntimeError("The Ollama server returned an unexpected response.")
        return payload

    @staticmethod
    def _checked_endpoint(endpoint: str, *, allow_remote: bool) -> str:
        if not isinstance(endpoint, str):
            raise TypeError("endpoint must be a URL string")
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(
                "endpoint must be an http(s) URL such as http://127.0.0.1:11434"
            )
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(
                "endpoint must not contain credentials, a query, or a fragment"
            )
        if not allow_remote and not OllamaOcrService._loopback(parts.hostname):
            raise ValueError(
                "endpoint is not on this machine, so page images would leave it; "
                "pass allow_remote=True to allow that explicitly."
            )
        return endpoint.rstrip("/")

    @staticmethod
    def _loopback(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
