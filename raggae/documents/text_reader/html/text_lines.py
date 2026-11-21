"""Assemble the lines of an HTML block the way a browser collapses whitespace."""

import re


class TextLines:
    """Assemble lines the way a browser collapses whitespace outside <pre>."""

    _COLLAPSIBLE = re.compile(r"[ \t\n\r\f]+")

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._current = ""

    @staticmethod
    def collapse(text: str) -> str:
        """Runs of HTML whitespace become one space."""
        return TextLines._COLLAPSIBLE.sub(" ", text)

    def inline(self, text: str) -> None:
        text = self.collapse(text)
        if not self._current or self._current.endswith(" "):
            text = text.lstrip(" ")
        self._current += text

    def hard_break(self) -> None:
        self._lines.append(self._current.rstrip(" "))
        self._current = ""

    def soft_break(self) -> None:
        if self._current.strip(" "):
            self.hard_break()
        self._current = ""

    def verbatim(self, text: str | None) -> None:
        self.soft_break()
        if text is not None:
            self._lines.append(text)

    def result(self) -> str | None:
        self.soft_break()
        lines = self._lines
        while lines and not lines[0].strip():
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        return "\n".join(lines) if lines else None
