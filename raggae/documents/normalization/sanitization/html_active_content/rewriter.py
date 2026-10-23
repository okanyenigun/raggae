"""Internal HTML parsing, selected content removal, and reconstruction."""

from collections import Counter
from html import escape
from html.parser import HTMLParser
from .models import HtmlActiveContentCategory


class HtmlRewriter(HTMLParser):
    """Small removal profile; deliberately not a browser-equivalent parser."""

    # Newer HTMLParser versions treat iframe bodies as raw text. This profile
    # needs their nested tags and ancestor end tags to reach our drop stack,
    # including recovery when an outer object closes an unclosed iframe.
    # Keep the parser's raw-text handling for scripts, styles, and other tags.
    CDATA_CONTENT_ELEMENTS = tuple(
        tag for tag in HTMLParser.CDATA_CONTENT_ELEMENTS if tag != "iframe"
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.removals: Counter[HtmlActiveContentCategory] = Counter()
        self._dropped: list[str] = []

    def _drop_element(self, tag: str, *, self_closing: bool = False) -> bool:
        if tag not in {"script", "iframe", "object", "embed"}:
            return False
        category = (
            HtmlActiveContentCategory.SCRIPT
            if tag == "script"
            else HtmlActiveContentCategory.EMBEDDED_FRAME
        )
        self.removals[category] += 1
        # embed is a void element: never suppress the following ordinary content.
        if tag != "embed" and not self_closing:
            self._dropped.append(tag)
        return True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_element(tag) or self._dropped:
            return
        self._append_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_element(tag, self_closing=True) or self._dropped:
            return
        self._append_tag(tag, attrs, self_closing=True)

    def _append_tag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        kept = []
        for name, value in attrs:
            if name.startswith("on"):
                self.removals[HtmlActiveContentCategory.EVENT_HANDLER] += 1
            elif (
                name in {"href", "src"}
                and value is not None
                and self._blocked_reference(value)
            ):
                self.removals[HtmlActiveContentCategory.REMOTE_REFERENCE] += 1
            else:
                kept.append((name, value))
        original = self.get_starttag_text()
        if len(kept) == len(attrs) and original is not None:
            self.parts.append(original)
            return
        attributes = "".join(
            f" {name}" if value is None else f' {name}="{escape(value, quote=True)}"'
            for name, value in kept
        )
        ending = " />" if self_closing else ">"
        self.parts.append(f"<{tag}{attributes}{ending}")

    def handle_endtag(self, tag: str) -> None:
        if self._dropped:
            if tag in self._dropped:
                index = len(self._dropped) - 1 - self._dropped[::-1].index(tag)
                del self._dropped[index:]
            return
        if tag not in {"script", "iframe", "object", "embed"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._dropped:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._dropped:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._dropped:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self._dropped:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self._dropped:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if not self._dropped:
            self.parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        if not self._dropped:
            self.parts.append(f"<![{data}]>")

    @staticmethod
    def _blocked_reference(value: str) -> bool:
        # HTMLParser decodes attribute entities. Browsers also ignore these whitespace
        # characters within schemes; examine them without modifying retained values.
        cleaned = value.strip().replace("\t", "").replace("\r", "").replace("\n", "")
        if cleaned.startswith(("//", "\\\\", "/\\", "\\/")):
            return True
        return cleaned.partition(":")[0].casefold() in {
            "http",
            "https",
            "ftp",
            "file",
            "mailto",
            "javascript",
            "data",
        }
