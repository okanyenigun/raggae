from io import BytesIO
from pathlib import Path
import warnings

from PIL import Image
import pytest

from raggae.documents.validation.checks.utils import (
    applicability,
    make_finding,
    normalize_extension,
    pillow_limit_lifted,
    require_path,
    unreadable,
)


@pytest.mark.parametrize("as_string", [False, True])
def test_require_path_accepts_paths_without_accessing_disk(
    missing_path, as_string, monkeypatch, forbidden_operation,
):
    monkeypatch.setattr(Path, "stat", forbidden_operation)
    monkeypatch.setattr(Path, "open", forbidden_operation)
    value = str(missing_path) if as_string else missing_path
    result = require_path(value)
    assert result == missing_path
    assert isinstance(result, Path)
    if not as_string:
        assert result is value


@pytest.mark.parametrize("value", [None, b"document.pdf", 7, object(), BytesIO(b"file")])
def test_require_path_rejects_unsupported_types(value):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        require_path(value)


@pytest.mark.parametrize("value,expected", [
    ("pdf", "pdf"), (" .PDF ", "pdf"), ("...PdF", "pdf"),
    ("JPG", "jpeg"), (" .TiF ", "tiff"), ("jpeg", "jpeg"),
    ("", ""), ("  ", ""), (".", ""), (" .UNKNOWN ", "unknown"),
])
def test_normalize_extension_is_canonical_and_idempotent(value, expected):
    assert normalize_extension(value) == expected
    assert normalize_extension(normalize_extension(value)) == expected


def test_make_finding_preserves_scalar_details(scalar_facts):
    finding = make_finding("custom.code", "Explanation.", **scalar_facts)
    assert finding.code == "custom.code"
    assert finding.message == "Explanation."
    assert dict(finding.detail) == scalar_facts


@pytest.mark.parametrize("fmt", [None, "jpeg", " .JPEG ", ".JpG"])
def test_applicability_accepts_matching_alias_or_no_claim(fmt):
    assert applicability(frozenset({"jpeg"}), fmt, "image probe", "custom.skip") == []


def test_applicability_reports_normalized_mismatch_and_sorted_handles():
    findings = applicability(
        frozenset({"png", "jpeg"}), " .PDF ", "image probe", "custom.skip",
    )
    assert len(findings) == 1
    assert findings[0].code == "custom.skip"
    assert dict(findings[0].detail) == {"detected_format": "pdf"}
    assert "image probe covers jpeg, png" in findings[0].message


def test_unreadable_reports_basename_and_underlying_error(tmp_path):
    finding = unreadable("custom.unreadable", tmp_path / "private" / "résumé.pdf", OSError("denied"))
    assert finding.code == "custom.unreadable"
    assert dict(finding.detail) == {"path": "résumé.pdf"}
    assert "denied" in finding.message
    assert str(tmp_path) not in finding.message


@pytest.mark.parametrize("previous", [123, None])
@pytest.mark.parametrize("raise_inside", [False, True])
def test_pillow_ceiling_is_lifted_and_always_restored(monkeypatch, previous, raise_inside):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", previous)
    warning_filters = warnings.filters[:]
    if raise_inside:
        with pytest.raises(RuntimeError, match="test failure"):
            with pillow_limit_lifted():
                assert Image.MAX_IMAGE_PIXELS is None
                raise RuntimeError("test failure")
    else:
        with pillow_limit_lifted():
            assert Image.MAX_IMAGE_PIXELS is None
    assert Image.MAX_IMAGE_PIXELS == previous
    assert warnings.filters == warning_filters


def test_nested_pillow_ceiling_contexts_restore_outer_then_original(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 456)
    with pillow_limit_lifted():
        with pillow_limit_lifted():
            assert Image.MAX_IMAGE_PIXELS is None
        assert Image.MAX_IMAGE_PIXELS is None
    assert Image.MAX_IMAGE_PIXELS == 456
