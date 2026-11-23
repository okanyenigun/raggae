"""Pictures written inside an HTML document, standalone or by layout region."""

import sys
from base64 import b64encode
from io import BytesIO

import pytest
from PIL import Image

from raggae.documents.image_extractor import (
    HtmlImageExtractor,
    ImageExtractor,
    ImageExtractResult,
)
from raggae.documents.layout_analysis import (
    HtmlDocumentLocation,
    HtmlElementLocation,
    LayoutAnalysisResult,
    LayoutRegion,
    LayoutSurface,
)
from raggae.documents.layout_analysis.region_discovery.html import HtmlLayoutAnalyzer
from raggae.documents.normalization import Artifact
from tests.documents.image_extractor.conftest import make_layout


def encoded(format="PNG", color="red"):
    output = BytesIO()
    Image.new("RGB", (4, 3), color).save(output, format)
    return output.getvalue()


PNG, JPEG = encoded(), encoded("JPEG", "blue")


def url(data, media_type="image/png"):
    return f"data:{media_type};base64,{b64encode(data).decode()}"


def codes(result):
    return [(f.code, f.region_id) for f in result.findings]


def summary(result):
    return [(p.region_id, p.media_type, p.method, p.alt_text) for p in result.images]


def extract(html, **options):
    data = html.encode() if isinstance(html, str) else html
    return HtmlImageExtractor().extract(data, **options)


PAGE = f"""<!doctype html>
<html><head><title>Pump</title></head>
<body>
<h1>Pump manual</h1>
<p>The pump: <img src="{url(PNG)}" alt="Pump"></p>
<figure><img src="{url(JPEG, "image/jpeg")}" alt=""><figcaption>Logo</figcaption></figure>
<p>Wiring: <img src="photos/wiring.png" alt="Wiring"></p>
</body></html>"""


def test_package_exports_the_worker():
    from raggae.documents.image_extractor import html

    assert html.HtmlImageExtractor is HtmlImageExtractor


@pytest.mark.parametrize("source_kind", ["path", "string", "bytes", "artifact"])
def test_sources_schema_protocol_and_unchanged_file(tmp_path, source_kind):
    data = PAGE.encode()
    path = tmp_path / "manual.html"
    path.write_bytes(data)
    source = {
        "path": path,
        "string": str(path),
        "bytes": data,
        "artifact": Artifact(data=data, password="private-password"),
    }[source_kind]
    extractor = HtmlImageExtractor()
    assert isinstance(extractor, ImageExtractor)
    assert extractor.name == "image_extractor_html"
    result = extractor.extract(source)
    assert result.outcome == "success"
    # An empty alt marks a decorative picture; it is kept as written.
    assert summary(result) == [
        (None, "image/png", "original", "Pump"),
        (None, "image/jpeg", "original", ""),
    ]
    assert result.images[0].data == PNG and result.images[0].page_index == 0
    assert codes(result) == [("reference_unresolved", None)]
    assert result.findings[0].message.endswith(": photos/wiring.png")
    assert ImageExtractResult.model_validate_json(result.model_dump_json()) == result
    assert "private-password" not in result.model_dump_json()
    assert path.read_bytes() == data and list(tmp_path.iterdir()) == [path]


# Which pictures ------------------------------------------------------------------


def test_never_read_hidden_and_non_picture_elements():
    html = f"""<html><head><script>x = '<img src="{url(PNG)}">'</script></head><body>
<template><img src="{url(PNG)}"></template>
<noscript><img src="{url(PNG)}"></noscript>
<div hidden><p><img src="{url(PNG)}" alt="hidden"></p></div>
<svg width="10" height="10"><rect width="5" height="5"/></svg>
<canvas></canvas>
<img src="{url(PNG)}" alt="visible">
</body></html>"""
    result = extract(html)
    assert result.outcome == "success"
    assert [p.alt_text for p in result.images] == ["visible"]
    assert codes(result) == [("hidden_skipped", None), ("unsupported_content", None)]


def test_references_are_never_fetched_and_base_grants_nothing(tmp_path):
    (tmp_path / "logo.png").write_bytes(PNG)
    path = tmp_path / "page.html"
    path.write_text(
        '<base href="https://cdn.example.com/"><img src="logo.png">'
        '<img src="https://user:secret@example.com/a.png?token=x">'
        "<img>"
        '<picture><source srcset="big.webp"><img src="small.png"></picture>'
    )
    result = HtmlImageExtractor().extract(path)
    assert result.outcome == "success" and not result.images
    messages = [f.message for f in result.findings]
    assert [code for code, _ in codes(result)] == ["reference_unresolved"] * 4
    assert messages[0].endswith(": logo.png")
    assert messages[1].endswith(": https://example.com/a.png")
    assert messages[2] == "A picture has no target."
    assert messages[3].endswith(": small.png")
    assert "secret" not in result.model_dump_json() and "token" not in result.model_dump_json()


def test_formats_and_broken_data():
    html = (
        f'<img src="{url(encoded("BMP"), "image/bmp")}" alt="bmp">'
        '<img src="data:image/svg+xml,%3Csvg/%3E" alt="svg">'
        '<img src="data:image/png;base64,***" alt="broken">'
    )
    result = extract(html)
    assert result.outcome == "partial"
    assert summary(result) == [(None, "image/png", "reencoded", "bmp")]
    assert [code for code, _ in codes(result)] == [
        "unsupported_image_type",
        "image_unreadable",
    ]


def test_documents_without_pictures_including_empty_ones():
    for html in ("<p>Words only.</p>", ""):
        result = extract(html)
        assert result.outcome == "success" and codes(result) == [("empty", None)]


def test_encoding_is_configured_never_guessed():
    data = f'<meta charset="cp1252"><img src="{url(PNG)}" alt="Café">'.encode("cp1252")
    assert HtmlImageExtractor(encoding="cp1252").extract(data).images[0].alt_text == "Café"
    failed = HtmlImageExtractor().extract(data)
    assert failed.outcome == "failed" and codes(failed) == [("decode_failed", None)]


@pytest.mark.parametrize("encoding", [None, 1, "latin-9"])
def test_encoding_option_is_validated(encoding):
    with pytest.raises((TypeError, ValueError)):
        HtmlImageExtractor(encoding=encoding)


def test_missing_file_and_missing_beautifulsoup(tmp_path, monkeypatch):
    assert codes(extract(tmp_path / "missing.html")) == [("source_unreadable", None)]
    monkeypatch.setitem(sys.modules, "bs4", None)
    with pytest.raises(ImportError, match=r"raggae\[text-html\]"):
        extract(PAGE)


# Layout regions --------------------------------------------------------------------


@pytest.fixture
def layout():
    return HtmlLayoutAnalyzer().analyze(PAGE.encode())


def ids(layout, tag):
    return [r.id for r in layout.regions if r.location.tag == tag]


def test_layout_links_each_picture_to_its_image_region(layout):
    images = ids(layout, "img")
    result = extract(PAGE, layout=layout)
    assert summary(result) == [
        (images[0], "image/png", "original", "Pump"),
        (images[1], "image/jpeg", "original", ""),
    ]
    assert codes(result) == [("reference_unresolved", images[2])]


def test_explicit_regions_and_the_innermost_owner(layout):
    first_paragraph, heading = ids(layout, "p")[0], ids(layout, "h1")[0]
    result = extract(PAGE, layout=layout, region_ids=[first_paragraph, heading])
    assert summary(result) == [(first_paragraph, "image/png", "original", "Pump")]
    assert codes(result) == [("empty", heading)]
    image = ids(layout, "img")[0]
    nested = extract(PAGE, layout=layout, region_ids=[first_paragraph, image])
    assert [p.region_id for p in nested.images] == [image]
    assert not nested.findings


def custom(*regions):
    return LayoutAnalysisResult(
        outcome="success",
        surfaces=(
            LayoutSurface(
                id_="document", kind="flow", location=HtmlDocumentLocation(element="document")
            ),
        ),
        regions=tuple(
            LayoutRegion(
                id=identifier,
                surface_id="document",
                kind=kind,
                location=HtmlElementLocation(child_indexes=path, tag=tag),
            )
            for identifier, kind, path, tag in regions
        ),
    )


def test_unresolved_unsupported_hidden_and_overlapping_regions():
    html = (
        f'<html><head><title>t</title></head><body><div hidden><img src="{url(PNG)}"></div>'
        f'<svg></svg><img src="{url(PNG)}"></body></html>'
    )
    layout = custom(
        ("hidden", "image", (0, 1, 0, 0), "img"),
        ("vector", "unknown", (0, 1, 1), "svg"),
        ("title", "text", (0, 0, 0), "title"),
        ("wrong-tag", "image", (0, 1, 2), "p"),
        ("far", "image", (0, 1, 9), "img"),
        ("twin-a", "image", (0, 1, 2), "img"),
        ("twin-b", "image", (0, 1, 2), "img"),
    )
    # Asked for by ID: without region_ids only the image regions would be used.
    result = extract(html, layout=layout, region_ids=[r.id for r in layout.regions])
    assert result.outcome == "partial" and not result.images
    assert codes(result) == [
        ("hidden_skipped", "hidden"),
        ("selection_unsupported", "vector"),
        ("selection_unsupported", "title"),
        ("region_unresolved", "wrong-tag"),
        ("region_unresolved", "far"),
        ("selection_overlap", "twin-a"),
        ("selection_overlap", "twin-b"),
    ]


def test_non_html_locations_and_empty_selections(tmp_path):
    pdf_layout = make_layout(("photo", 1, "image", (0, 0, 10, 10)))
    assert codes(extract(PAGE, layout=pdf_layout)) == [("selection_unsupported", "photo")]
    missing = tmp_path / "missing.html"
    assert codes(HtmlImageExtractor().extract(missing, layout=pdf_layout, region_ids=[])) == [
        ("nothing_selected", None)
    ]
