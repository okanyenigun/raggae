"""Extract only a saved page's main article, as Markdown, with trafilatura."""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ..utils.sources import failure, prepare_source, read_source_text, require_encoding
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


type ArticleFocus = Literal["balanced", "precision", "recall"]


class ArticleHtmlTextReader:
    """Return a page's main article as one Markdown block.

    Navigation, footers, sidebars, and reader comments are left out; headings,
    lists, tables, and emphasis are kept as Markdown. ``focus`` selects
    trafilatura's trade-off: ``"precision"`` drops borderline text, ``"recall"``
    keeps more. Nothing is fetched or executed; title, author, and date
    metadata are not extracted. Layout-region requests are unsupported: this
    reader chooses the article itself.
    """

    def __init__(
        self, *, focus: ArticleFocus = "balanced", encoding: TextEncoding = "utf-8"
    ) -> None:
        if not isinstance(focus, str):
            raise TypeError("focus must be a string")
        if focus not in ("balanced", "precision", "recall"):
            raise ValueError("focus must be 'balanced', 'precision', or 'recall'")
        self._focus: ArticleFocus = focus
        self._encoding: TextEncoding = require_encoding(encoding)

    @property
    def name(self) -> str:
        return "text_reader_article_html"

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
            unsupported_message=(
                "ArticleHtmlTextReader extracts the main article, not layout regions."
            ),
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        text = read_source_text(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(text, TextReadResult):
            return text
        try:
            import trafilatura
            from lxml.etree import LxmlError
            from trafilatura.settings import use_config
        except ImportError as error:
            raise ImportError(
                "Article extraction requires: pip install 'raggae[text-html]'"
            ) from error
        # A private config, never trafilatura's shared default. Below its usual
        # 250-character minimum, trafilatura swaps the main extractor's
        # structured result for a baseline that runs text together; a
        # minimum of 1 always keeps the structured result (agreed).
        config = use_config()
        config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "1")
        try:
            # extract() only parses the given markup; it never downloads.
            article = trafilatura.extract(
                text,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
                include_images=False,
                include_links=False,
                with_metadata=False,
                deduplicate=False,
                favor_precision=self._focus == "precision",
                favor_recall=self._focus == "recall",
                config=config,
            )
        except (LxmlError, ValueError, TypeError, RecursionError):
            return failure(
                self.name,
                FindingCode.PARSE_FAILED,
                "Source could not be parsed as HTML.",
            )
        if not article or not article.strip():
            return TextReadResult(
                reader=self.name,
                outcome="success",
                findings=(
                    TextReadFinding(
                        code=FindingCode.EMPTY,
                        message="No main article text was found.",
                        page_index=0,
                    ),
                ),
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=(
                TextBlock(
                    text=article,
                    page_index=0,
                    content_format=TextContentFormat.MARKDOWN,
                ),
            ),
        )
