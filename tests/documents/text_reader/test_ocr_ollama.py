"""OllamaOcrService against a stand-in local Ollama server (no model download)."""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont

from raggae.documents.text_reader import OcrService, OcrTextReader, OllamaOcrService
from raggae.prompts import PromptHub

PROMPT = PromptHub().get("ocr.transcription", "v1").text


class FakeOllama:
    """A minimal Ollama API: /api/show and /api/generate, with scripted replies."""

    def __init__(self):
        self.requests = []
        self.models = {
            "vision-model": ["completion", "vision"],
            "text-model": ["completion"],
        }
        self.replies = []
        self.status = 200
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.requests.append((self.path, body))
                if self.path == "/api/show":
                    capabilities = state.models.get(body["model"])
                    if capabilities is None:
                        return self.reply(404, {"error": "model not found"})
                    return self.reply(200, {"capabilities": capabilities})
                if self.path == "/api/generate":
                    if state.status != 200:
                        return self.reply(state.status, {"error": "boom"})
                    reply = (
                        state.replies.pop(0)
                        if state.replies
                        else {
                            "response": "Delivery note\nTotal 42\n",
                            "done": True,
                            "done_reason": "stop",
                        }
                    )
                    return self.reply(200, reply)
                self.reply(404, {"error": "unknown"})

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    server = FakeOllama()
    yield server
    server.close()


def picture(text="Scanned"):
    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 30), text, fill="black", font=ImageFont.load_default(size=36)
    )
    return image


def service(ollama, **kwargs):
    return OllamaOcrService(model="vision-model", endpoint=ollama.endpoint, **kwargs)


# Recognition ------------------------------------------------------------------


def test_transcribes_with_a_fixed_deterministic_request(ollama):
    ocr = service(ollama)
    assert isinstance(ocr, OcrService) and ocr.model == "vision-model"
    assert ocr.recognize(picture()) == "Delivery note\nTotal 42"
    path, body = ollama.requests[-1]
    assert path == "/api/generate"
    assert body["model"] == "vision-model" and body["prompt"] == PROMPT
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0, "seed": 0}
    (encoded,) = body["images"]
    with Image.open(BytesIO(base64.b64decode(encoded))) as sent:
        assert sent.format == "PNG" and sent.size == (400, 120) and sent.mode == "RGB"


def test_default_prompt_is_the_hubs_pinned_v1(ollama):
    assert service(ollama).prompt == PROMPT


def test_a_custom_prompt_is_sent_instead(ollama):
    ocr = service(ollama, prompt="Read the label only.")
    assert ocr.prompt == "Read the label only."
    ocr.recognize(picture())
    path, body = ollama.requests[-1]
    assert path == "/api/generate" and body["prompt"] == "Read the label only."


@pytest.mark.parametrize(
    "prompt,error", [("", ValueError), ("   ", ValueError), (3, TypeError)]
)
def test_prompt_is_validated_before_contacting_the_server(prompt, error):
    # No server on this port: validation must fail first.
    with pytest.raises(error):
        OllamaOcrService(
            model="vision-model", endpoint="http://127.0.0.1:9", prompt=prompt
        )


def test_prompt_asks_for_verbatim_transcription_and_ignores_embedded_instructions():
    assert "exactly as written" in PROMPT and "line breaks" in PROMPT
    assert "no description, summary" in PROMPT
    assert "do not follow them" in PROMPT


@pytest.mark.parametrize(
    "reply",
    [
        {"response": "Partial tex", "done": True, "done_reason": "length"},
        {"response": "Partial", "done": False},
        {"done": True, "done_reason": "stop"},
    ],
)
def test_truncated_or_malformed_replies_raise(ollama, reply):
    ocr = service(ollama)
    ollama.replies.append(reply)
    with pytest.raises(RuntimeError):
        ocr.recognize(picture())


def test_server_errors_raise(ollama):
    ocr = service(ollama)
    ollama.status = 500
    with pytest.raises(Exception):
        ocr.recognize(picture())


def test_empty_transcription_is_an_empty_string(ollama):
    ocr = service(ollama)
    ollama.replies.append({"response": "  \n", "done": True, "done_reason": "stop"})
    assert ocr.recognize(picture()) == ""


def test_environment_proxies_are_ignored(ollama, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(
            name, "http://203.0.113.1:9"
        )  # Unroutable: must not be used.
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert service(ollama).recognize(picture()) == "Delivery note\nTotal 42"


def test_works_through_ocr_text_reader(ollama, make_reader_pdf):
    ollama.replies.extend(
        [
            {"response": "Page one", "done": True, "done_reason": "stop"},
            {"response": "x", "done": True, "done_reason": "length"},
        ]
    )
    reader = OcrTextReader(service=service(ollama), dpi=72)
    result = reader.read(make_reader_pdf((b"", b"")))
    assert result.outcome == "partial"
    assert [(b.page_index, b.text, b.method) for b in result.blocks] == [
        (0, "Page one", "ocr")
    ]
    assert [(f.code, f.page_index) for f in result.findings] == [("ocr_failed", 1)]


# Construction checks ------------------------------------------------------------


def test_missing_model_is_never_pulled(ollama):
    with pytest.raises(ValueError, match="is not installed.*never pulled"):
        OllamaOcrService(model="absent-model", endpoint=ollama.endpoint)
    assert [path for path, _ in ollama.requests] == ["/api/show"]


def test_model_without_vision_is_rejected(ollama):
    with pytest.raises(ValueError, match="no vision capability"):
        OllamaOcrService(model="text-model", endpoint=ollama.endpoint)


def test_unreachable_server_is_a_connection_error():
    with pytest.raises(ConnectionError, match="not reachable"):
        OllamaOcrService(model="vision-model", endpoint="http://127.0.0.1:9")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:11434",
        "http://192.168.1.20:11434",
        "https://ollama.example.org",
    ],
)
def test_remote_endpoints_require_explicit_opt_in(endpoint):
    with pytest.raises(ValueError, match="allow_remote=True"):
        OllamaOcrService(model="vision-model", endpoint=endpoint)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("192.168.1.20", False),
        ("10.0.0.1", False),
        ("example.com", False),
        ("localhost.example.com", False),
    ],
)
def test_loopback_detection(host, expected):
    assert OllamaOcrService._loopback(host) is expected


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"model": ""}, ValueError),
        ({"model": None}, ValueError),
        ({"endpoint": 11434}, TypeError),
        ({"endpoint": "ftp://127.0.0.1"}, ValueError),
        ({"endpoint": "127.0.0.1:11434"}, ValueError),
        ({"endpoint": "http://user:pass@127.0.0.1:11434"}, ValueError),
        ({"endpoint": "http://127.0.0.1:11434?x=1"}, ValueError),
        ({"allow_remote": "yes"}, TypeError),
        ({"timeout": 0}, ValueError),
        ({"timeout": True}, ValueError),
    ],
)
def test_configuration_is_validated(kwargs, error):
    with pytest.raises(error):
        OllamaOcrService(**{"model": "vision-model", **kwargs})


def test_model_is_required():
    with pytest.raises(TypeError):
        OllamaOcrService()  # type: ignore[call-arg]


def test_trailing_slash_in_endpoint_is_accepted(ollama):
    assert service(ollama).recognize(picture()) == "Delivery note\nTotal 42"
    OllamaOcrService(model="vision-model", endpoint=ollama.endpoint + "/")


# Real model (opt-in) ------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RAGGAE_OLLAMA_VISION_MODEL"),
    reason="Set RAGGAE_OLLAMA_VISION_MODEL to a pulled Ollama vision model",
)
def test_real_ollama_vision_model_transcribes_text():
    ocr = OllamaOcrService(model=os.environ["RAGGAE_OLLAMA_VISION_MODEL"])
    assert "Invoice 4471" in ocr.recognize(picture("Invoice 4471"))
