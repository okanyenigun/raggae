"""The shared image-result fields are immutable and serialize consistently."""

from base64 import b64decode

import pytest
from pydantic import ValidationError

from raggae.documents.image_extractor import (
    ExtractedImage,
    ImageExtractFinding,
    ImageExtractResult,
    ImageFindingCode,
)
from raggae.documents.text_reader import FindingCode

# Not valid UTF-8, as real image bytes usually are not.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n\xff\xfe"


def image(**options):
    fields = {
        "data": PNG_SIGNATURE,
        "media_type": "image/png",
        "width": 2,
        "height": 3,
        "method": "reencoded",
        **options,
    }
    return ExtractedImage(**fields)


def finding(**options):
    return ImageExtractFinding(code=ImageFindingCode.EMPTY, message="Example.", **options)


@pytest.mark.parametrize(
    "media_type", ["image/png", "image/jpeg", "image/tiff", "image/gif", "image/webp"]
)
@pytest.mark.parametrize("method", ["original", "reencoded", "rendered"])
def test_common_result_shape_and_json_roundtrip(media_type, method):
    result = ImageExtractResult(
        extractor="example",
        outcome="success",
        images=(image(media_type=media_type, method=method, alt_text="Pump"),),
    )
    restored = ImageExtractResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.images[0].data == PNG_SIGNATURE


def test_defaults_and_field_sets_carry_no_location_or_geometry():
    result = ImageExtractResult(extractor="example", outcome="success", images=(image(),))
    assert set(result.model_dump()) == {"extractor", "outcome", "images", "findings"}
    assert set(result.images[0].model_dump()) == {
        "data",
        "media_type",
        "width",
        "height",
        "page_index",
        "region_id",
        "method",
        "alt_text",
    }
    picture = result.images[0]
    assert picture.page_index == 0
    assert picture.region_id is None and picture.alt_text is None
    assert finding().page_index is None and finding().region_id is None


@pytest.mark.parametrize(
    "options",
    [
        {"location": {"page": 1}},
        {"bbox": (0, 0, 10, 10)},
        {"media_type": "image/bmp"},
        {"method": "embedded"},
        {"method": None},
        {"data": b""},
        {"width": 0},
        {"height": -1},
        {"width": True},
        {"width": 2.0},
        {"page_index": -1},
        {"page_index": "1"},
        {"region_id": ""},
    ],
)
def test_invalid_image_fields_are_rejected(options):
    with pytest.raises(ValidationError):
        image(**options)


def test_method_is_required():
    with pytest.raises(ValidationError):
        ExtractedImage(data=PNG_SIGNATURE, media_type="image/png", width=1, height=1)


def test_an_empty_alt_text_is_kept_as_the_source_wrote_it():
    assert image(alt_text="").alt_text == ""


def test_to_base64_returns_standard_base64_of_the_image_bytes():
    # These bytes encode to "+/8=" in standard base64 and "-_8=" in the URL-safe form.
    picture = image(data=b"\xfb\xff")
    assert picture.to_base64() == "+/8="
    assert b64decode(image().to_base64(), validate=True) == PNG_SIGNATURE


def test_to_base64_is_not_a_field():
    assert "to_base64" not in image().model_dump()


def test_repr_does_not_print_image_bytes():
    assert "data" not in repr(image()) and "PNG" not in repr(image())


@pytest.mark.parametrize(
    "model,field,value",
    [
        (image(), "data", b"edited"),
        (image(), "page_index", 1),
        (finding(), "message", "edited"),
        (ImageExtractResult(extractor="example", outcome="success"), "outcome", "failed"),
    ],
)
def test_models_are_frozen(model, field, value):
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(model, field, value)


@pytest.mark.parametrize("outcome", ["partial", "failed", "not_applicable"])
def test_incomplete_result_requires_a_finding(outcome):
    with pytest.raises(ValidationError, match="explanation"):
        ImageExtractResult(extractor="example", outcome=outcome)


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_unusable_result_cannot_contain_images(outcome):
    with pytest.raises(ValidationError, match="cannot contain images"):
        ImageExtractResult(
            extractor="example",
            outcome=outcome,
            images=(image(),),
            findings=(finding(),),
        )


def test_partial_result_may_hold_no_images():
    result = ImageExtractResult(
        extractor="example", outcome="partial", findings=(finding(page_index=2),)
    )
    assert result.findings[0].page_index == 2


@pytest.mark.parametrize(
    "options", [{"message": ""}, {"page_index": -1}, {"region_id": ""}, {"extra": 1}]
)
def test_invalid_finding_fields_are_rejected(options):
    fields = {"code": ImageFindingCode.EMPTY, "message": "Example.", **options}
    with pytest.raises(ValidationError):
        ImageExtractFinding(**fields)


def test_codes_shared_with_text_readers_keep_the_same_value():
    shared = [code for code in ImageFindingCode if code.name in FindingCode.__members__]
    assert shared, "expected codes shared with the text readers"
    for code in shared:
        assert code.value == FindingCode[code.name].value
