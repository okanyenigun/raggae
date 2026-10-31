"""Native HTML element locations, coverage boundaries, and no browser execution."""

from raggae.documents.layout_analysis import HtmlLayoutPolicy

import builtins
import os
import subprocess
import sys
import warnings

import pytest
from pydantic import ValidationError

bs4 = pytest.importorskip("bs4")
from bs4.element import Tag

from raggae.documents.layout_analysis import (
    DocxElementLocation, GeometricReadingOrderResolver, HtmlDocumentLocation,
    HtmlElementLocation, HtmlLayoutAnalyzer, LayoutAnalysisResult,
    LayoutAnalyzer, LayoutRegion, LayoutSurface,
)
from raggae.documents.normalization import Artifact


SOURCE = b'''<!doctype html>
<html><head><meta charset="utf-8"><title>Not a body heading</title></head>
<body><h1>Warehouse report</h1><p>A paragraph with <strong>inline emphasis</strong>.</p>
<table><tr><th>Item</th><th>Count</th></tr><tr><td>Box</td><td>12</td></tr></table>
<figure><img src="fixture.png" alt="not extracted"><figcaption>Stock image caption</figcaption></figure>
<p>Closing paragraph.</p></body></html>'''


def resolve(document, location):
    for index in location.child_indexes:
        document = [child for child in document.children if isinstance(child, Tag)][index]
    assert document.name == location.tag
    return document


@pytest.mark.parametrize("source_kind", ["bytes", "path", "string", "artifact_bytes", "artifact_path"])
@pytest.mark.parametrize("positional_source", [False, True])
def test_all_public_inputs_share_immutable_serializable_map(tmp_path, source_kind, positional_source):
    path = tmp_path / "report.bin"
    path.write_bytes(SOURCE)
    source = {"bytes": SOURCE, "path": path, "string": str(path), "artifact_bytes": Artifact(data=SOURCE),
              "artifact_path": Artifact(path=path)}[source_kind]
    worker = HtmlLayoutAnalyzer()
    result = worker.analyze(source) if positional_source else worker.analyze(source=source)
    assert result.outcome == "success"
    assert [region.kind for region in result.regions] == ["heading", "text", "table", "image", "caption", "text"]
    assert [region.native_order for region in result.regions] == list(range(6))
    assert all(region.bbox is None for region in result.regions)
    surface, = result.surfaces
    assert surface.kind == "flow" and isinstance(surface.location, HtmlDocumentLocation)
    assert surface.width is surface.height is surface.unit is None
    assert "Warehouse report" not in result.model_dump_json()
    assert "fixture.png" not in result.model_dump_json()
    assert "not extracted" not in result.model_dump_json()
    assert LayoutAnalysisResult.model_validate_json(result.model_dump_json()) == result
    assert isinstance(worker, LayoutAnalyzer) and worker.name == "layout_analysis_html"
    assert path.read_bytes() == SOURCE
    assert GeometricReadingOrderResolver().resolve_order(result).outcome == "not_applicable"


def test_locators_resolve_exact_elements_ignoring_comments_and_whitespace():
    result = HtmlLayoutAnalyzer().analyze(source=SOURCE)
    document = bs4.BeautifulSoup(SOURCE.decode("utf-8"), "html.parser")
    assert [resolve(document, region.location).name for region in result.regions] == ["h1", "p", "table", "img", "figcaption", "p"]
    assert result.regions[1].location.child_indexes == (0, 1, 1)
    data = b'<!-- comment --><p>First <b>bold</b></p>\n<!-- another --><p>Second</p>'
    result = HtmlLayoutAnalyzer().analyze(source=data)
    assert [region.location.child_indexes for region in result.regions] == [(0,), (1,)]


def test_inline_markup_does_not_duplicate_text_but_retains_images():
    result = HtmlLayoutAnalyzer().analyze(source=b'<p>One <strong>bold</strong> tail<img src="x"></p>')
    assert result.outcome == "success"
    assert [region.kind for region in result.regions] == ["text", "image"]


def test_tables_are_atomic_with_separate_embedded_image_locations():
    source = b'<table><caption>Caption within table</caption><tr><td><p>Cell<img src="x"></p><table><tr><td>Nested</td></tr></table></td></tr></table>'
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "success"
    assert [region.kind for region in result.regions] == ["table", "image"]
    document = bs4.BeautifulSoup(source, "html.parser")
    assert resolve(document, result.regions[1].location).name == "img"


def test_lists_and_fallback_inline_text_have_resolvable_regions():
    source = b'<ul><li>First</li><li>Second</li></ul><div>A <b>bold</b></div><section><span>Inline</span></section>'
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "success"
    assert [region.location.tag for region in result.regions] == ["li", "li", "div", "span"]


def test_mixed_container_reports_overlapping_scope_instead_of_dropping_text():
    result = HtmlLayoutAnalyzer().analyze(source=b'<div>Before<p>Child paragraph</p>After</div>')
    assert result.outcome == "partial"
    assert [region.kind for region in result.regions] == ["unknown", "text"]
    assert result.findings[0].code == "layout.html.mixed_container_text"


def test_nested_text_blocks_are_not_silently_claimed_as_complete():
    result = HtmlLayoutAnalyzer().analyze(source=b'<blockquote>Outer<p>Nested</p></blockquote>')
    assert result.outcome == "partial" and len(result.regions) == 1
    assert result.findings[0].code == "layout.html.nested_text_blocks"


def test_scripts_and_remote_references_are_not_executed_or_fetched(monkeypatch):
    import socket
    import urllib.request

    def forbidden(*args, **kwargs):
        raise AssertionError("network access must not occur")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    source = b'''<html><head><script src="https://invalid.example/code.js">raise Error()</script>
    <link rel="stylesheet" href="https://invalid.example/style.css"></head><body>
    <img src="https://invalid.example/image.png"><iframe src="https://invalid.example/frame"></iframe>
    <p>Stored text</p></body></html>'''
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "partial"
    assert [region.kind for region in result.regions] == ["image", "unknown", "text"]
    assert {finding.code for finding in result.findings} == {"layout.html.dynamic_content", "layout.html.styling_uninterpreted", "layout.html.unsupported_content"}
    assert "invalid.example" not in result.model_dump_json()


def test_conditional_vector_and_hidden_content_have_explicit_boundaries():
    source = b'<template><p>Template text</p></template><noscript><p>Conditional</p></noscript><svg><text>Vector</text></svg><p hidden>Stored hidden text</p>'
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "partial"
    assert [region.location.tag for region in result.regions] == ["svg", "p"]
    assert {finding.code for finding in result.findings} == {"layout.html.conditional_content", "layout.html.unsupported_content", "layout.html.hidden_content"}


def test_css_does_not_change_native_order_or_create_rectangles():
    source = b'<div style="display:flex;flex-direction:column-reverse"><p>First stored</p><p>Second stored</p></div>'
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "partial"
    document = bs4.BeautifulSoup(source, "html.parser")
    assert [resolve(document, region.location).get_text() for region in result.regions] == ["First stored", "Second stored"]
    assert [region.native_order for region in result.regions] == [0, 1]
    assert all(region.bbox is None for region in result.regions)


@pytest.mark.parametrize("source", [b'', b' \n\t', b'<!doctype html>', b'<html><head><title>Title only</title></head><body></body></html>'])
def test_blank_content_is_a_valid_empty_flow(source):
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "success" and len(result.surfaces) == 1 and not result.regions


def test_plain_text_without_an_element_locator_is_reported():
    result = HtmlLayoutAnalyzer().analyze(source=b'Plain text fragment')
    assert result.outcome == "partial" and not result.regions
    assert result.findings[0].code == "layout.html.unlocated_text"


def test_utf8_bom_entities_and_unclosed_fragment_are_supported():
    data = b'\xef\xbb\xbf<h1>Caf\xc3\xa9 &amp; tea</h1><p>Open paragraph'
    result = HtmlLayoutAnalyzer().analyze(source=data)
    assert result.outcome == "success"
    assert [region.kind for region in result.regions] == ["heading", "text"]
    document = bs4.BeautifulSoup(data.decode("utf-8-sig"), "html.parser")
    assert resolve(document, result.regions[0].location).get_text() == "Café & tea"


@pytest.mark.parametrize("source, code", [(b'<p>caf\xe9</p>', 'encoding_unsupported'), (b'\xff\xfeh\x00', 'encoding_unsupported'), (b'<p>\x00</p>', 'decode_failed')])
def test_invalid_encoding_is_not_guessed_or_replaced(source, code):
    result = HtmlLayoutAnalyzer().analyze(source=source)
    assert result.outcome == "failed" and not result.surfaces
    assert result.findings[0].code == "layout.html." + code


def test_limits_and_exact_byte_boundary():
    source = b'<p>First</p><p>Second</p>'
    assert HtmlLayoutAnalyzer(policy=HtmlLayoutPolicy(max_source_bytes=len(source))).analyze(source=source).outcome == "success"
    assert HtmlLayoutAnalyzer(policy=HtmlLayoutPolicy(max_source_bytes=len(source)-1)).analyze(source=source).outcome == "failed"
    for option, code in (("max_elements", "element_limit"), ("max_regions", "region_limit")):
        result = HtmlLayoutAnalyzer(policy=HtmlLayoutPolicy(**{option: 1})).analyze(source=source)
        assert result.outcome == "partial" and len(result.regions) == 1
        assert result.findings[0].code == "layout.html." + code
    result = HtmlLayoutAnalyzer(policy=HtmlLayoutPolicy(max_depth=2)).analyze(source=b'<div><section><p>Deep</p></section></div>')
    assert result.outcome == "partial" and not result.regions
    assert result.findings[0].code == "layout.html.depth_limit"


@pytest.mark.parametrize("name", ["max_source_bytes", "max_elements", "max_depth", "max_regions"])
@pytest.mark.parametrize("value, error", [(True, ValidationError), (1.5, ValidationError), (0, ValidationError), (-1, ValidationError)])
def test_constructor_limits(name, value, error):
    with pytest.raises(error):
        HtmlLayoutAnalyzer(policy=HtmlLayoutPolicy(**{name: value}))


def test_missing_directory_and_nonregular_sources(tmp_path):
    worker = HtmlLayoutAnalyzer()
    assert worker.analyze(source=tmp_path / "missing.html").outcome == "failed"
    assert worker.analyze(source=tmp_path).outcome == "failed"
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "source.html"
        os.mkfifo(fifo)
        assert worker.analyze(source=fifo).outcome == "failed"


def test_duplicate_missing_or_invalid_sources_are_rejected():
    worker = HtmlLayoutAnalyzer()
    with pytest.raises(TypeError):
        worker.analyze(SOURCE, source=SOURCE)
    with pytest.raises(TypeError):
        worker.analyze()
    with pytest.raises(TypeError):
        worker.analyze({})


def test_backend_warning_is_reported_without_leaking_details(monkeypatch):
    original = bs4.BeautifulSoup

    def warned(*args, **kwargs):
        warnings.warn("private backend diagnostics", UserWarning)
        return original(*args, **kwargs)

    monkeypatch.setattr(bs4, "BeautifulSoup", warned)
    result = HtmlLayoutAnalyzer().analyze(source=SOURCE)
    assert result.outcome == "partial"
    assert result.findings[0].code == "layout.html.parser_coverage"
    assert "private backend diagnostics" not in result.model_dump_json()


def test_missing_backend_has_installation_hint(monkeypatch):
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] == "bs4":
            raise ImportError("not installed")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ImportError, match="layout-html"):
        HtmlLayoutAnalyzer().analyze(source=SOURCE)


def test_public_import_does_not_require_optional_html_backend():
    code = '''
import builtins
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] == 'bs4':
        raise ImportError('external backend unavailable')
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from raggae.documents.layout_analysis import HtmlLayoutAnalyzer
assert HtmlLayoutAnalyzer().name == 'layout_analysis_html'
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_html_models_forbid_geometry_and_wrong_flow_locators():
    location = HtmlDocumentLocation(element="document")
    with pytest.raises(ValidationError):
        LayoutSurface(id_="html", kind="flow", location=location, width=10)
    surface = LayoutSurface(id_="html", kind="flow", location=location)
    region = LayoutRegion(id="r1", surface_id="html", kind="text", location=DocxElementLocation(part="word/document.xml", child_indexes=(0,)))
    with pytest.raises(ValidationError):
        LayoutAnalysisResult(outcome="success", surfaces=(surface,), regions=(region,))
    with pytest.raises(ValidationError):
        HtmlElementLocation(child_indexes=(-1,), tag="p")
