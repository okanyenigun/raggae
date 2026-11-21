"""Shared plumbing for the Unstructured-backed readers.

Each wrapper (in the folder of its format) returns one plain-text block per
Unstructured element (Title, NarrativeText, ListItem, Table, Header, Footer,
...), with ``page_index`` from the element's page or slide or sheet number where
the format has one, and ``0`` otherwise. Tables are written as rows on lines
with cells joined by `` | ``. Empty elements (such as page breaks) give no block.
Layout-region requests are unsupported. Requires ``raggae[text-unstructured]``
and the spaCy model ``en_core_web_sm``.

Unstructured 0.27.8 sends analytics to packages.unstructured.io when it is
imported and on every partition call, unless ``DO_NOT_TRACK`` or
``SCARF_NO_ANALYTICS`` is set. It also downloads and installs the spaCy model
``en_core_web_sm`` into site-packages at run time when that model is missing.
These readers set Unstructured's own opt-out before importing it and refuse to
run without the spaCy model, so a read never contacts the network or modifies
the environment.
"""

import importlib.util
import os
from collections.abc import Sequence
from pathlib import Path
from stat import S_ISREG
from typing import Any
from .models import FindingCode, TextBlock, TextReadFinding, TextReadResult
from .utils.sources import failure, prepare_source, read_source_text, require_encoding
from .utils.unstructured_text import element_text
from ..layout_analysis.models import LayoutAnalysisResult
from ..normalization.artifacts import Artifact
from ..normalization.models import TextEncoding


class UnstructuredReader:
    """Run one Unstructured ``partition_*`` function; one block per element."""

    _SPACY_MODEL = "en_core_web_sm"

    _name: str
    _label: str
    _binary: bool = False

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return self._name

    def _partition(self, source: str | bytes) -> list[Any]:
        raise NotImplementedError

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        prepared = prepare_source(
            source,
            reader=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message=f"{self._label} reads whole documents, not layout regions.",
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        loaded = self._load(prepared)
        if isinstance(loaded, TextReadResult):
            return loaded
        UnstructuredReader._load_backend()
        return self._convert(loaded)

    def _load(self, prepared: Artifact) -> str | bytes | TextReadResult:
        if not self._binary:
            return read_source_text(prepared, encoding=self._encoding, reader=self.name)
        try:
            if prepared.path is not None and not S_ISREG(prepared.path.stat().st_mode):
                return self._failure(
                    FindingCode.SOURCE_UNREADABLE, "Source must be a regular file."
                )
            data = prepared.read_bytes()
        except OSError:
            return self._failure(
                FindingCode.SOURCE_UNREADABLE, "Source could not be read."
            )
        if not data:
            return self._failure(FindingCode.DECODE_FAILED, "Source is empty.")
        return data

    def _convert(self, loaded: str | bytes) -> TextReadResult:
        try:
            elements = self._partition(loaded)
        # Unstructured raises many unrelated types for unreadable input.
        except Exception:  # noqa: BLE001
            return self._failure(
                FindingCode.PARSE_FAILED, "Unstructured could not partition the source."
            )
        blocks = [
            TextBlock(text=text, page_index=UnstructuredReader._page_index(element))
            for element in elements
            if (text := element_text(element))
        ]
        findings = (
            ()
            if blocks
            else (
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="Unstructured found no text in the source.",
                    page_index=0,
                ),
            )
        )
        return TextReadResult(
            reader=self.name, outcome="success", blocks=tuple(blocks), findings=findings
        )

    def _failure(self, code: FindingCode, message: str) -> TextReadResult:
        return failure(self.name, code, message)

    @staticmethod
    def _load_backend() -> None:
        """Opt out of Unstructured's telemetry, then check the spaCy model is present."""
        opted_out = (os.environ.get("DO_NOT_TRACK") or "").strip() or (
            os.environ.get("SCARF_NO_ANALYTICS") or ""
        ).strip()
        if not opted_out:
            # Unstructured's documented opt-out; it must be set before the import
            # and stay set, because every partition call checks it again.
            os.environ["SCARF_NO_ANALYTICS"] = "true"
        try:
            import unstructured  # noqa: F401
        except ImportError as error:
            raise ImportError(
                "Unstructured readers require: pip install 'raggae[text-unstructured]'"
            ) from error
        if importlib.util.find_spec(UnstructuredReader._SPACY_MODEL) is None:
            raise RuntimeError(
                f"Unstructured readers need the spaCy model {UnstructuredReader._SPACY_MODEL}, which "
                "Unstructured would otherwise download and install by itself. "
                f"Install it once: python -m spacy download {UnstructuredReader._SPACY_MODEL}"
            )

    @staticmethod
    def _page_index(element: Any) -> int:
        number = getattr(element.metadata, "page_number", None)
        return number - 1 if isinstance(number, int) and number >= 1 else 0
