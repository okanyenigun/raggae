"""Locate selected HTML metadata tags without rebuilding the document."""

from html.parser import HTMLParser


class MetadataParser(HTMLParser):
    """Record exact tag spans; do not certify browser parsing or document privacy."""

    _NAMES = frozenset({"author", "creator", "description", "generator", "keywords"})

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self.removals: list[tuple[int, int]] = []
        self._line_starts = [0]
        self._line_starts.extend(
            index + 1 for index, char in enumerate(text) if char == "\n"
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        # Encoding declarations take precedence, even on unusual mixed-purpose tags.
        if any(
            name == "charset"
            or (
                name == "http-equiv"
                and (value or "").strip().casefold() == "content-type"
            )
            for name, value in attrs
        ):
            return
        if not any(
            name in {"name", "property"}
            and (value or "").strip().casefold() in self._NAMES
            for name, value in attrs
        ):
            return
        raw_tag = self.get_starttag_text()
        if raw_tag is None:
            raise ValueError("missing source tag span")
        line, column = self.getpos()
        start = self._line_starts[line - 1] + column
        self.removals.append((start, start + len(raw_tag)))
