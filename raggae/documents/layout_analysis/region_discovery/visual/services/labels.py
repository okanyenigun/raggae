"""Translate model labels into the SDK's deliberately small region catalog."""

from ....models import RegionKind


def region_kind(label: str) -> RegionKind:
    label = label.lower().replace("-", "_").replace(" ", "_")
    if label in {
        "title",
        "doc_title",
        "document_title",
        "paragraph_title",
        "section_header",
    }:
        return "heading"
    if label in {
        "caption",
        "figure_caption",
        "table_caption",
        "figure_title",
        "figure_table_title",
        "table_title",
        "chart_title",
    }:
        return "caption"
    if label == "table":
        return "table"
    if label in {"image", "picture", "figure", "chart", "header_image", "footer_image"}:
        return "image"
    if label in {
        "text",
        "list",
        "list_item",
        "abstract",
        "reference",
        "references",
        "reference_content",
        "footnote",
        "footnotes",
        "header",
        "footer",
        "page_header",
        "page_footer",
        "page_number",
        "number",
        "sidebar_text",
        "aside_text",
        "code",
        "algorithm",
        "content",
        "document_index",
    }:
        return "text"
    return "unknown"
