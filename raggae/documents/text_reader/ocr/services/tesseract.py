"""Local Tesseract OCR through pytesseract; never downloads language data."""

import re
import shutil
from math import isfinite
from pathlib import Path
from PIL import Image


class TesseractOcrService:
    """Recognize text with the locally installed Tesseract engine.

    ``language`` is required (for example ``"eng"`` or ``"deu+eng"``); each
    language's data must already be installed, which is checked at
    construction. ``timeout`` (seconds) limits one recognition; the default
    ``None`` imposes no limit. ``executable`` selects a Tesseract binary;
    otherwise ``tesseract`` is looked up on ``PATH``.
    """

    _LANGUAGE = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*")

    def __init__(
        self,
        *,
        language: str,
        timeout: float | None = None,
        executable: Path | str | None = None,
    ) -> None:
        if not isinstance(language, str):
            raise TypeError("language must be a string such as 'eng' or 'deu+eng'")
        if not self._LANGUAGE.fullmatch(language):
            raise ValueError("language must look like 'eng' or 'deu+eng'")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number of seconds or None")
        if executable is not None and not isinstance(executable, (Path, str)):
            raise TypeError("executable must be a path or None")
        try:
            import pytesseract
        except ImportError as error:
            raise ImportError(
                "Tesseract OCR requires: pip install 'raggae[text-tesseract]' "
                "and the Tesseract engine (for example: brew install tesseract)"
            ) from error
        command = (
            str(executable) if executable is not None else shutil.which("tesseract")
        )
        if command is None or shutil.which(command) is None:
            raise FileNotFoundError(
                "The Tesseract engine was not found; install it or pass executable=..."
            )
        self._command = command
        try:
            installed = set(self._call(pytesseract.get_languages))
        except (pytesseract.TesseractError, OSError, RuntimeError) as error:
            raise RuntimeError("The Tesseract engine could not be run.") from error
        missing = [code for code in language.split("+") if code not in installed]
        if missing:
            raise ValueError(
                f"Tesseract language data {', '.join(repr(code) for code in missing)} "
                "is not installed; install it locally (it is never downloaded)."
            )
        self._language = language
        self._timeout = timeout

    @property
    def language(self) -> str:
        return self._language

    def recognize(self, image: Image.Image) -> str:
        import pytesseract

        return self._call(
            pytesseract.image_to_string,
            image,
            lang=self._language,
            timeout=self._timeout or 0,
        )

    def _call(self, function, *args, **kwargs):
        """Run pytesseract against this service's binary, then restore its default."""
        import pytesseract

        module = pytesseract.pytesseract
        previous = module.tesseract_cmd
        module.tesseract_cmd = self._command
        try:
            return function(*args, **kwargs)
        finally:
            module.tesseract_cmd = previous
