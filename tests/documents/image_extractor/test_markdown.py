"""Pictures written inside a Markdown document; references to other files reported."""

import sys
from base64 import b64encode
from io import BytesIO

import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    ImageExtractor,
    ImageExtractResult,
    MarkdownImageExtractor,
)
from raggae.documents.image_extractor.utils import describe
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import make_layout


def encoded(format="PNG", color="red", size=(4, 3), mode="RGB"):
    output = BytesIO()
    Image.new(mode, size, color).save(output, format)
    return output.getvalue()


def data_url(data, media_type="image/png"):
    return f"data:{media_type};base64,{b64encode(data).decode()}"


def codes(result):
    return [(f.code, f.page_index) for f in result.findings]


def summary(result):
    return [(p.media_type, p.method, p.alt_text) for p in result.images]


def extract(text, **options):
    data = text.encode() if isinstance(text, str) else text
    return MarkdownImageExtractor().extract(data, **options)


PNG = encoded()


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import markdown

    assert markdown.MarkdownImageExtractor is MarkdownImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = f"# Manual\n\n![Pump]({data_url(PNG)})\n".encode()
    path = tmp_path / "manual.md"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = MarkdownImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_markdown"
    result = extractor.extract(source)
    assert result.outcome == "success" and not result.findings
    [picture] = result.images
    assert picture.data == PNG and (picture.width, picture.height) == (4, 3)
    assert (picture.page_index, picture.region_id) == (0, None)
    assert summary(result) == [("image/png", "original", "Pump")]
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Pictures written inside the document ----------------------------------------


def test_every_kind_of_markdown_image_in_order():
    jpeg, gif = encoded("JPEG"), encoded("GIF")
    text = f"""
![First]({data_url(PNG)})

[![Linked]({data_url(jpeg, "image/jpeg")})](https://example.com)

| a | b |
|---|---|
| ![In a table]({data_url(gif, "image/gif")}) | x |

![By reference][figure]

    ![In code]({data_url(PNG)})

![First]({data_url(PNG)})

[figure]: {data_url(PNG)}
"""
    result = extract(text)
    assert result.outcome == "success" and not result.findings
    assert summary(result) == [
        ("image/png", "original", "First"),
        ("image/jpeg", "original", "Linked"),
        ("image/gif", "original", "In a table"),
        ("image/png", "original", "By reference"),
        ("image/png", "original", "First"),
    ]


def test_types_the_markdown_link_check_would_hide():
    # markdown-it's default check drops data:image/tiff; it is turned off here.
    tiff = encoded("TIFF")
    [picture] = extract(f"![Scan]({data_url(tiff, 'image/tiff')})").images
    assert (picture.data, picture.media_type) == (tiff, "image/tiff")


def test_other_formats_and_broken_data():
    bmp = encoded("BMP")
    svg = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http://www.w3.org/2000/svg%22/%3E"
    text = f"""
![bmp]({data_url(bmp, "image/bmp")})
![svg]({svg})
![broken](data:image/png;base64,not*base64)
![text](data:text/plain;base64,aGVsbG8=)
"""
    result = extract(text)
    assert result.outcome == "partial"
    assert summary(result) == [("image/png", "reencoded", "bmp")]
    assert codes(result) == [
        ("unsupported_image_type", 0),
        ("image_unreadable", 0),
        ("unsupported_image_type", 0),
    ]


def test_percent_encoded_data_urls_are_decoded():
    escaped = "".join(f"%{byte:02X}" for byte in PNG)
    [picture] = extract(f"![Escaped](data:image/png,{escaped})").images
    assert picture.data == PNG


# References to other files -----------------------------------------------------


def test_references_are_reported_and_never_opened(tmp_path):
    (tmp_path / "pump.png").write_bytes(PNG)
    path = tmp_path / "manual.md"
    path.write_text(
        "![Pump](pump.png)\n"
        "![Spaced](<photos/my pump.png>)\n"
        "![Web](https://user:secret@example.com/logo.png?token=abc#top)\n"
        "![Empty]()\n"
    )
    result = MarkdownImageExtractor().extract(path)
    assert result.outcome == "success" and not result.images
    messages = [f.message for f in result.findings]
    assert codes(result) == [("reference_unresolved", 0)] * 4
    assert messages[0].endswith(": pump.png")
    assert messages[1].endswith(": photos/my pump.png")
    assert messages[2].endswith(": https://example.com/logo.png")
    assert messages[3] == "A picture has no target."
    dumped = result.model_dump_json()
    assert "secret" not in dumped and "token" not in dumped and "user" not in dumped


def test_long_targets_are_shortened():
    assert len(describe("x" * 500 + ".png")) == 200


def test_raw_html_pictures_are_reported():
    result = extract(
        f'<img src="raw.png">\n\nText with <IMG src="a.png"> inline.\n\n![md]({data_url(PNG)})'
    )
    assert summary(result) == [("image/png", "original", "md")]
    assert codes(result) == [("unsupported_content", None)]


def test_documents_without_pictures_including_empty_ones():
    for text in ("# Only words\n\nNo pictures.", ""):
        result = extract(text)
        assert result.outcome == "success" and codes(result) == [("empty", None)]


# Encoding, size, and sources ---------------------------------------------------


def test_encoding_is_configured_never_guessed():
    text = f"![Café]({data_url(PNG)})".encode("cp1252")
    assert (
        MarkdownImageExtractor(encoding="cp1252").extract(text).images[0].alt_text
        == "Café"
    )
    failed = MarkdownImageExtractor().extract(text)
    assert failed.outcome == "failed" and codes(failed) == [("decode_failed", None)]


@pytest.mark.parametrize("encoding", [None, 1, "latin-9"])
def test_encoding_option_is_validated(encoding):
    with pytest.raises((TypeError, ValueError)):
        MarkdownImageExtractor(encoding=encoding)


def test_a_picture_above_pillows_ceiling_is_skipped(monkeypatch):
    monkeypatch.setattr(
        Image, "MAX_IMAGE_PIXELS", 3
    )  # ceiling: 6 pixels; this PNG has 12
    result = extract(f"![Big]({data_url(PNG)})")
    assert result.outcome == "partial" and codes(result) == [("image_too_large", 0)]


def test_missing_file(tmp_path):
    result = MarkdownImageExtractor().extract(tmp_path / "missing.md")
    assert codes(result) == [("source_unreadable", None)]


def test_layouts_are_refused_without_opening_the_file(tmp_path):
    layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    missing = tmp_path / "missing.md"
    result = MarkdownImageExtractor().extract(missing, layout=layout)
    assert result.outcome == "failed"
    assert [(f.code, f.region_id) for f in result.findings] == [
        ("selection_unsupported", "photo")
    ]
    empty = MarkdownImageExtractor().extract(missing, layout=layout, region_ids=[])
    assert [(f.code, f.region_id) for f in empty.findings] == [
        ("selection_unsupported", None)
    ]


def test_missing_markdown_it_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "markdown_it", None)
    with pytest.raises(ImportError, match=r"raggae\[text-markdown\]"):
        extract("![x](y.png)")
