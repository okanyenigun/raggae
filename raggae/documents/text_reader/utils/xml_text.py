"""XML 1.0 text rules shared by the readers that write XML blocks."""

import re


class XmlText:
    """Valid tag names, representable characters, and escaping for XML content."""

    def __init__(self) -> None:
        # XML 1.0 names without a colon (namespace-safe), and the characters XML 1.0 allows.
        start = (
            "A-Z_a-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02ff\u0370-\u037d\u037f-\u1fff"
            "\u200c\u200d\u2070-\u218f\u2c00-\u2fef\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd"
            "\U00010000-\U000effff"
        )
        self._name_pattern = re.compile(
            f"[{start}][{start}\\-.0-9\u00b7\u0300-\u036f\u203f-\u2040]*"
        )
        self._illegal_pattern = re.compile(
            "[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
        )

    def is_name(self, name: str) -> bool:
        """Whether ``name`` can be an element name."""
        return self._name_pattern.fullmatch(name) is not None

    def has_illegal(self, text: str) -> bool:
        """Whether ``text`` holds a character XML 1.0 cannot represent."""
        return self._illegal_pattern.search(text) is not None

    def element(self, name: str, value: str) -> str:
        """``<name>value</name>``, or ``<name/>`` for an empty value."""
        if not value:
            return f"<{name}/>"
        escaped = (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\r", "&#13;")
        )
        return f"<{name}>{escaped}</{name}>"

    def attribute(self, name: str, value: str) -> str:
        """`` name="value"`` with every character that could end or alter it escaped."""
        escaped = (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("\t", "&#9;")
            .replace("\n", "&#10;")
            .replace("\r", "&#13;")
        )
        return f' {name}="{escaped}"'
