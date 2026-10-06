import os
from pathlib import Path
import zipfile

import pikepdf
from PIL import Image
import pypdf
import pytest

from raggae.documents.validation import (
    ActiveContentPolicy, CapabilityPolicy, ContentTypePolicy, FilenamePolicy, IdentityPolicy,
)
from raggae.documents.validation.checks.active_content.no import NoOpActiveContentProbe
from raggae.documents.validation.checks.capability.no import NoOpCapabilityProbe
from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe
from raggae.documents.validation.checks.resource_limits.no import NoOpResourceProbe
from raggae.documents.validation.schemas.result import CheckOutcome


NO_OPS = [(NoOpFilenameValidator, FilenamePolicy, "filename"),
          (NoOpIdentityProbe, IdentityPolicy, "identity"),
          (NoOpContentTypeDetector, ContentTypePolicy, "content_type"),
          (NoOpResourceProbe, object, "format"),
          (NoOpActiveContentProbe, ActiveContentPolicy, "format"),
          (NoOpCapabilityProbe, CapabilityPolicy, "format")]


@pytest.fixture
def no_io(monkeypatch, missing_path, forbidden_operation):
    # Restore guards before tmp_path cleanup; only each no-op call is in scope.
    with monkeypatch.context() as guard:
        for method in ("open", "read_text", "read_bytes", "stat", "lstat"):
            guard.setattr(Path, method, forbidden_operation)
        guard.setattr(os, "open", forbidden_operation)
        guard.setattr(Image, "open", forbidden_operation)
        guard.setattr(pikepdf.Pdf, "open", forbidden_operation)
        guard.setattr(pypdf, "PdfReader", forbidden_operation)
        guard.setattr(zipfile, "ZipFile", forbidden_operation)
        yield missing_path


def arguments(shape, value):
    if shape == "filename":
        return {"filename": value}
    if shape == "identity":
        return {"path": value}
    if shape == "content_type":
        return {"path": value, "claimed_extension": "pdf"}
    return {"path": value, "detected_format": "pdf", "password": "unused-password"}


def assert_empty(result):
    assert isinstance(result, CheckOutcome)
    assert result.findings == ()
    assert result.codes == ()
    assert dict(result.facts) == {}


@pytest.mark.parametrize("factory,policy_factory,shape", NO_OPS)
@pytest.mark.parametrize("policy_mode", ["default", "none", "custom"])
@pytest.mark.parametrize("keyword", [False, True])
def test_no_op_constructor_and_calls_without_io(no_io, factory, policy_factory, shape, policy_mode, keyword):
    supplied = policy_factory() if policy_mode == "custom" else None
    worker = factory() if policy_mode == "default" else factory(policy=supplied)
    if policy_mode == "custom":
        assert worker._policy is supplied
    values = arguments(shape, "../../bad.py" if shape == "filename" else no_io)
    result = worker.check(**values) if keyword else worker.check(*values.values())
    assert_empty(result)


@pytest.mark.parametrize("factory,policy_factory,shape", NO_OPS)
@pytest.mark.parametrize("bad_input", [None, b"not-a-string-or-path", 17])
def test_no_op_deliberately_does_not_validate_input(no_io, factory, policy_factory, shape, bad_input):
    assert_empty(factory().check(**arguments(shape, bad_input)))


@pytest.mark.parametrize("factory,policy_factory,shape", NO_OPS)
def test_no_op_repeated_calls_produce_independent_empty_outcomes(no_io, factory, policy_factory, shape):
    worker = factory()
    values = arguments(shape, "document.pdf" if shape == "filename" else no_io)
    first = worker.check(**values)
    second = worker.check(**values)
    assert_empty(first)
    assert_empty(second)
    assert first is not second
    with pytest.raises(TypeError):
        first.facts["invented"] = True
    assert dict(second.facts) == {}
