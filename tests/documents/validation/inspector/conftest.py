from types import SimpleNamespace

import pytest

from raggae.documents.validation import DocumentInspector
from raggae.documents.validation.schemas.result import CheckOutcome


class RecordingWorker:
    """A structural worker double that never opens a document."""

    def __init__(self, name, calls, *, facts=None, handles=frozenset({"pdf"})):
        self.name = name
        self.handles = handles
        self.outcome = CheckOutcome(facts=facts or {})
        self.calls = calls

    def check(self, *args, **kwargs):
        self.calls.append((self.name, args, kwargs))
        return self.outcome


@pytest.fixture
def inspector_factory():
    def make_inspector(**overrides):
        calls = []
        workers = {
            "filename": RecordingWorker("test_filename", calls, facts={"claimed_extension": "pdf"}),
            "identity": RecordingWorker("test_identity", calls, facts={"size_bytes": 7}),
            "content_type": RecordingWorker("test_content_type", calls, facts={"detected_format": "pdf"}),
            "encryption": RecordingWorker("test_encryption", calls),
            "resource": RecordingWorker("test_resource", calls),
            "active_content": RecordingWorker("test_active_content", calls),
            "capability": RecordingWorker("test_capability", calls),
        }
        dependencies = {
            "filename_validator": workers["filename"],
            "identity_probe": workers["identity"],
            "content_type_detector": workers["content_type"],
            "encryption_probe": workers["encryption"],
            "resource_probes": (workers["resource"],),
            "active_content_probes": (workers["active_content"],),
            "capability_probes": workers["capability"],
        }
        dependencies.update(overrides)
        return SimpleNamespace(
            inspector=DocumentInspector(**dependencies), workers=workers, calls=calls,
        )

    return make_inspector
