"""HTML readers using the common text result model."""

from .article import ArticleHtmlTextReader
from .elements import HtmlTextReader
from .unstructured import UnstructuredHtmlTextReader

__all__ = ["UnstructuredHtmlTextReader", "ArticleHtmlTextReader", "HtmlTextReader"]
