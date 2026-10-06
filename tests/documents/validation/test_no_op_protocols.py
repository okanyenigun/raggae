import pytest

from raggae.documents.validation.checks.active_content.base import ActiveContentProbe
from raggae.documents.validation.checks.active_content.no import NoOpActiveContentProbe
from raggae.documents.validation.checks.capability.base import CapabilityProbe
from raggae.documents.validation.checks.capability.no import NoOpCapabilityProbe
from raggae.documents.validation.checks.content_type.base import ContentTypeDetector
from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.base import FilenameValidator
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.base import IdentityProbe
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe
from raggae.documents.validation.checks.resource_limits.base import ResourceValidator
from raggae.documents.validation.checks.resource_limits.no import NoOpResourceProbe


@pytest.mark.parametrize("factory,protocol,format_worker", [
    pytest.param(NoOpFilenameValidator, FilenameValidator, False, id="filename"),
    pytest.param(NoOpIdentityProbe, IdentityProbe, False, id="identity"),
    pytest.param(NoOpContentTypeDetector, ContentTypeDetector, False, id="content-type"),
    pytest.param(NoOpResourceProbe, ResourceValidator, True, id="resource"),
    pytest.param(NoOpActiveContentProbe, ActiveContentProbe, True, id="active-content"),
    pytest.param(NoOpCapabilityProbe, CapabilityProbe, True, id="capability"),
])
def test_no_op_can_substitute_for_its_worker_protocol(factory, protocol, format_worker):
    worker = factory()
    assert isinstance(worker, protocol)
    assert isinstance(worker.name, str) and worker.name.strip()
    if format_worker:
        assert isinstance(worker.handles, frozenset)
