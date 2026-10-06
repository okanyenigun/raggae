from types import SimpleNamespace

import pytest

from raggae.documents.validation import (
    ArchiveResourceProbe, CsvActiveContentProbe, FileIdentityProbe, HtmlActiveContentProbe,
    ImageResourceProbe, OoxmlActiveContentProbe, PdfActiveContentProbe, PdfCapabilityProbe,
    PdfEncryptionProbe, PdfResourceProbe, SignatureContentTypeDetector,
    StrictFilenameValidator, TextResourceProbe,
)
from raggae.documents.validation.checks.active_content.base import ActiveContentProbe
from raggae.documents.validation.checks.capability.base import CapabilityProbe
from raggae.documents.validation.checks.content_type.base import ContentTypeDetector
from raggae.documents.validation.checks.encryption.base import EncryptionValidator
from raggae.documents.validation.checks.filename.base import FilenameValidator
from raggae.documents.validation.checks.identity.base import IdentityProbe
from raggae.documents.validation.checks.resource_limits.base import ResourceValidator
from raggae.documents.validation.checks.utils import normalize_extension
from raggae.documents.validation.schemas.result import CheckOutcome


PROTOCOLS = [(FilenameValidator, "filename"), (IdentityProbe, "identity"),
             (ContentTypeDetector, "content_type"), (EncryptionValidator, "format"),
             (ResourceValidator, "format"), (ActiveContentProbe, "format"),
             (CapabilityProbe, "format")]
WORKERS = [(StrictFilenameValidator, FilenameValidator, "filename"),
           (FileIdentityProbe, IdentityProbe, "identity"),
           (SignatureContentTypeDetector, ContentTypeDetector, "content_type"),
           (PdfEncryptionProbe, EncryptionValidator, "format"),
           (ArchiveResourceProbe, ResourceValidator, "format"),
           (ImageResourceProbe, ResourceValidator, "format"),
           (PdfResourceProbe, ResourceValidator, "format"),
           (TextResourceProbe, ResourceValidator, "format"),
           (PdfActiveContentProbe, ActiveContentProbe, "format"),
           (OoxmlActiveContentProbe, ActiveContentProbe, "format"),
           (HtmlActiveContentProbe, ActiveContentProbe, "format"),
           (CsvActiveContentProbe, ActiveContentProbe, "format"),
           (PdfCapabilityProbe, CapabilityProbe, "format")]


def call_arguments(shape, path):
    if shape == "filename":
        return {"filename": "document.txt"}
    if shape == "identity":
        return {"path": path}
    if shape == "content_type":
        return {"path": path, "claimed_extension": "txt"}
    return {"path": path, "detected_format": "unsupported", "password": "not-recorded"}


@pytest.mark.parametrize("factory,protocol,shape", WORKERS)
def test_concrete_workers_expose_protocol_metadata(factory, protocol, shape):
    worker = factory()
    assert isinstance(worker, protocol)
    assert isinstance(worker.name, str) and worker.name.strip()
    assert worker.name == factory().name
    if shape == "format":
        assert isinstance(worker.handles, frozenset) and worker.handles
        assert all(isinstance(fmt, str) and fmt and normalize_extension(fmt) == fmt for fmt in worker.handles)
        assert worker.handles == factory().handles


@pytest.mark.parametrize("factory,protocol,shape", WORKERS)
@pytest.mark.parametrize("keyword", [False, True])
def test_concrete_worker_accepts_actual_protocol_call(file_factory, factory, protocol, shape, keyword):
    path = file_factory("document.txt")
    worker = factory()
    arguments = call_arguments(shape, path)
    result = worker.check(**arguments) if keyword else worker.check(*arguments.values())
    assert isinstance(result, CheckOutcome)
    if shape == "format":
        assert len(result.findings) == 1
        assert result.codes[0].endswith(".not_applicable")
        assert dict(result.findings[0].detail) == {"detected_format": "unsupported"}


def test_default_worker_names_are_distinct():
    names = [factory().name for factory, _, _ in WORKERS]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("protocol,shape", PROTOCOLS)
def test_custom_structural_double_is_accepted_and_called(missing_path, protocol, shape):
    calls = []
    outcome = CheckOutcome(facts={"custom": True})

    def check(**kwargs):
        calls.append(kwargs)
        return outcome

    worker = SimpleNamespace(name="custom_worker", check=check, handles=frozenset({"pdf"}))
    assert isinstance(worker, protocol)
    arguments = call_arguments(shape, missing_path)
    assert worker.check(**arguments) is outcome
    assert calls == [arguments]


MISSING_MEMBERS = [(protocol, member) for protocol, shape in PROTOCOLS
                   for member in (["name", "handles", "check"] if shape == "format" else ["name", "check"])]


@pytest.mark.parametrize("protocol,missing_member", MISSING_MEMBERS)
def test_missing_required_member_rejects_structural_conformance(protocol, missing_member):
    members = {"name": "custom", "handles": frozenset({"pdf"}), "check": lambda *a, **kw: CheckOutcome()}
    del members[missing_member]
    assert not isinstance(SimpleNamespace(**members), protocol)


@pytest.mark.parametrize("protocol,shape", PROTOCOLS)
def test_runtime_conformance_does_not_guarantee_signature(missing_path, protocol, shape):
    worker = SimpleNamespace(name="wrong_signature", handles=frozenset({"pdf"}), check=lambda: CheckOutcome())
    assert isinstance(worker, protocol)
    with pytest.raises(TypeError):
        worker.check(**call_arguments(shape, missing_path))


@pytest.mark.parametrize("protocol,shape", PROTOCOLS)
def test_runtime_conformance_does_not_guarantee_return_type(missing_path, protocol, shape):
    worker = SimpleNamespace(name="wrong_return", handles=frozenset({"pdf"}), check=lambda **kwargs: "not-an-outcome")
    assert isinstance(worker, protocol)
    assert not isinstance(worker.check(**call_arguments(shape, missing_path)), CheckOutcome)


@pytest.mark.parametrize("protocol", [EncryptionValidator, ResourceValidator, ActiveContentProbe, CapabilityProbe])
def test_format_protocols_are_structural_not_exclusive_categories(protocol):
    worker = SimpleNamespace(name="custom_format", handles=frozenset({"pdf"}), check=lambda *a, **kw: CheckOutcome())
    assert isinstance(worker, protocol)
