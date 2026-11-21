"""The first shared text-result fields are immutable and serialize consistently."""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from raggae.documents.text_reader import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)


def block(**options):
    text = '{"name":"Café"}' if options.get("content_format") == "json" else "Café"
    return TextBlock(text=text, **options)


@pytest.mark.parametrize("content_format", ["plain_text", "markdown", "json", "xml"])
@pytest.mark.parametrize("method", ["native", "ocr", "vision", "mixed", "unknown"])
def test_common_result_shape_and_json_roundtrip(content_format, method):
    result = TextReadResult(
        reader="example",
        outcome="success",
        blocks=(block(content_format=content_format, method=method),),
    )
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result
    assert result.model_dump()["blocks"][0]["page_index"] == 0


@pytest.mark.parametrize(
    "model,field,value",
    [
        (block(), "page_index", 1),
        (block(), "text", "edited"),
        (
            TextReadFinding(code=FindingCode.EMPTY, message="Example."),
            "message",
            "edited",
        ),
        (TextReadResult(reader="example", outcome="success"), "outcome", "failed"),
    ],
)
def test_models_are_frozen(model, field, value):
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(model, field, value)


@pytest.mark.parametrize("outcome", ["partial", "failed", "not_applicable"])
def test_incomplete_result_requires_a_finding(outcome):
    with pytest.raises(ValidationError, match="explanation"):
        TextReadResult(reader="example", outcome=outcome)


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_result_cannot_contain_text(outcome):
    with pytest.raises(ValidationError, match="cannot contain text"):
        TextReadResult(
            reader="example",
            outcome=outcome,
            blocks=(block(),),
            findings=(TextReadFinding(code=FindingCode.EMPTY, message="Example."),),
        )


@pytest.mark.parametrize(
    "options",
    [
        {"content_format": "html"},
        {"method": "guess"},
        {"region_id": ""},
        {"unknown": 1},
    ],
)
def test_invalid_block_fields_are_rejected(options):
    with pytest.raises(ValidationError):
        block(**options)


def test_models_do_not_require_a_layout_or_store_source_bytes():
    result = TextReadResult(reader="example", outcome="success", blocks=(block(),))
    assert set(result.model_dump()) == {"reader", "outcome", "blocks", "findings"}
    assert result.blocks[0].region_id is None
    assert set(result.blocks[0].model_dump()) == {
        "text",
        "page_index",
        "region_id",
        "content_format",
        "method",
    }


@pytest.mark.parametrize("page_index", [0, 1, 100])
def test_page_indexes_roundtrip_for_blocks_and_findings(page_index):
    result = TextReadResult(
        reader="example",
        outcome="partial",
        blocks=(block(page_index=page_index),),
        findings=(
            TextReadFinding(
                code=FindingCode.EMPTY, message="Example.", page_index=page_index
            ),
        ),
    )
    assert result.blocks[0].page_index == result.findings[0].page_index == page_index
    assert TextReadResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("page_index", [-1, True, 1.5, "1"])
def test_page_indexes_must_be_nonnegative_integers(page_index):
    with pytest.raises(ValidationError):
        block(page_index=page_index)
    with pytest.raises(ValidationError):
        TextReadFinding(
            code=FindingCode.EMPTY, message="Example.", page_index=page_index
        )


def test_unknown_finding_page_is_none_not_an_assumed_first_page():
    finding = TextReadFinding(
        code=FindingCode.SOURCE_UNREADABLE, message="Cannot open source."
    )
    assert finding.page_index is None
    with pytest.raises(ValidationError):
        block(page_index=None)


@pytest.mark.parametrize(
    "options",
    [
        {"location": {"kind": "document"}},
        {"bbox": (0, 0, 100, 100)},
    ],
)
def test_blocks_do_not_accept_layout_geometry(options):
    with pytest.raises(ValidationError):
        block(**options)


def test_old_location_models_are_not_part_of_the_public_api():
    import raggae.documents.text_reader as public
    import raggae.documents.text_reader.models as models

    for name in ("DocumentTextLocation", "PdfTextLocation", "TextLocation"):
        assert not hasattr(public, name)
        assert not hasattr(models, name)


def test_public_import_does_not_load_optional_reader_backends():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from raggae.documents.text_reader import JsonTextReader, MarkdownTextReader, PlainTextReader, RawMarkdownTextReader, TextReader, WholeJsonTextReader, RecordJsonTextReader, SubtreeJsonTextReader, JqJsonTextReader, JsonLinesTextReader, JqJsonLinesTextReader
assert isinstance(PlainTextReader(), TextReader)
assert isinstance(MarkdownTextReader(), TextReader)
assert isinstance(RawMarkdownTextReader(), TextReader)
assert isinstance(JsonTextReader(), TextReader)
assert isinstance(WholeJsonTextReader(), TextReader)
assert isinstance(RecordJsonTextReader(), TextReader)
assert isinstance(SubtreeJsonTextReader(pointers=()), TextReader)
assert isinstance(JqJsonTextReader("."), TextReader)
assert isinstance(JsonLinesTextReader(), TextReader)
assert isinstance(JqJsonLinesTextReader("."), TextReader)
assert not any(name in sys.modules for name in (
    'markdown_it', 'pdfplumber', 'pandas', 'unstructured', 'paddleocr', 'torch', 'transformers',
    'docling', 'pymupdf', 'markitdown', 'ollama',
))
""",
        ],
        check=True,
    )


def test_finding_codes_are_bare_reasons_from_the_enum():
    finding = TextReadFinding(code="selection_unsupported", message="Example.")
    assert finding.code is FindingCode.SELECTION_UNSUPPORTED
    assert finding.code == "selection_unsupported"
    assert finding.model_dump(mode="json")["code"] == "selection_unsupported"
    for unknown in ("example", "text_read.selection_unsupported", ""):
        with pytest.raises(ValidationError):
            TextReadFinding(code=unknown, message="Example.")


def test_content_format_is_an_enum_and_rejects_unknown_values():
    assert block(content_format="markdown").content_format is TextContentFormat.MARKDOWN
    assert block().content_format is TextContentFormat.PLAIN_TEXT
    assert (
        block(content_format="json").model_dump(mode="json")["content_format"] == "json"
    )
    for unknown in ("html", "Markdown", ""):
        with pytest.raises(ValidationError):
            block(content_format=unknown)
