"""Pictures written inside a Markdown document, with markdown-it-py."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args
from ..models import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from ..utils import (
    HTML_PICTURE,
    failure,
    finish,
    picture_at,
    prepare_source,
    read_source,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class MarkdownImageExtractor:
    """Return the pictures written inside a Markdown document, with markdown-it-py.

    Every Markdown image (``![alt](target)``, reference-style images included,
    in tables and links too) is found in document order; image syntax inside
    code is not an image. A picture written inside the file as a data URL
    (``data:image/png;base64,...``) comes back as its file: PNG, JPEG, TIFF,
    GIF, and WebP byte for byte (``original``), other raster formats as new
    files (``reencoded``), SVG as ``unsupported_image_type``. ``alt_text`` is
    the image's alt text, and ``page_index`` is always 0.

    A picture in another file (``photos/pump.png``, ``https://...``) is never
    opened or fetched: an informational ``reference_unresolved`` names it,
    without user names, passwords, queries, or fragments. Targets are kept
    exactly as written; markdown-it's link checks and percent-encoding, meant
    for rendered HTML, are turned off. Pictures written as raw HTML inside the
    Markdown are not read (``unsupported_content``).

    The source is decoded with ``encoding`` (UTF-8 by default), never guessed.
    Markdown has no layout regions, so any layout is refused before the source
    is opened.
    """

    _INFORMATIONAL = frozenset(
        {
            ImageFindingCode.EMPTY,
            ImageFindingCode.REFERENCE_UNRESOLVED,
            ImageFindingCode.UNSUPPORTED_CONTENT,
        }
    )

    def __init__(self, *, encoding: TextEncoding = "utf-8") -> None:
        if not isinstance(encoding, str):
            raise TypeError("encoding must be a string")
        if encoding not in get_args(TextEncoding):
            raise ValueError(
                "encoding must be one of: " + ", ".join(get_args(TextEncoding))
            )
        self._encoding = encoding

    @property
    def name(self) -> str:
        return "image_extractor_markdown"

    def extract(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> ImageExtractResult:
        prepared = prepare_source(
            source,
            extractor=self.name,
            layout=layout,
            region_ids=region_ids,
            unsupported_message=(
                "Markdown has no layout regions; MarkdownImageExtractor reads whole "
                "documents."
            ),
        )
        if isinstance(prepared, ImageExtractResult):
            return prepared
        data = read_source(prepared, self.name, allow_empty=True)
        if isinstance(data, ImageExtractResult):
            return data
        try:
            text = data.decode(self._encoding, errors="strict")
        except UnicodeDecodeError:
            return failure(
                self.name,
                ImageFindingCode.DECODE_FAILED,
                "Source could not be decoded using the configured encoding.",
            )
        try:
            from markdown_it import MarkdownIt
        except ImportError as error:
            raise ImportError(
                "Markdown image extraction requires: pip install 'raggae[text-markdown]'"
            ) from error
        parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
        # Both defaults protect rendered HTML; nothing is rendered or fetched here.
        parser.validateLink = lambda url: True
        parser.normalizeLink = lambda url: url

        images: list[ExtractedImage] = []
        findings: list[ImageExtractFinding] = []
        found = 0
        html_pictures = False
        for token in MarkdownImageExtractor._tokens(parser.parse(text)):
            if token.type in ("html_block", "html_inline"):
                html_pictures = html_pictures or bool(
                    HTML_PICTURE.search(token.content)
                )
            elif token.type == "image":
                found += 1
                extracted = picture_at(
                    str(token.attrs.get("src", "")), alt_text=token.content
                )
                if isinstance(extracted, ExtractedImage):
                    images.append(extracted)
                else:
                    findings.append(extracted)
        if html_pictures:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.UNSUPPORTED_CONTENT,
                    message="Pictures written as HTML inside the Markdown were not read.",
                )
            )
        if not found:
            findings.append(
                ImageExtractFinding(
                    code=ImageFindingCode.EMPTY, message="No pictures in the document."
                )
            )
        return finish(self.name, images, findings, 1, self._INFORMATIONAL)

    @staticmethod
    def _tokens(tokens: list[Any]) -> list[Any]:
        """Every token in document order, inline children included."""
        flat: list[Any] = []
        pending = list(reversed(tokens))
        while pending:
            token = pending.pop()
            flat.append(token)
            if token.children:
                pending.extend(reversed(token.children))
        return flat
