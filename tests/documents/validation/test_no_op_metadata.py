import pytest

from raggae.documents.validation.checks.active_content.no import NoOpActiveContentProbe
from raggae.documents.validation.checks.capability.no import NoOpCapabilityProbe
from raggae.documents.validation.checks.content_type.no import NoOpContentTypeDetector
from raggae.documents.validation.checks.filename.no import NoOpFilenameValidator
from raggae.documents.validation.checks.identity.no import NoOpIdentityProbe
from raggae.documents.validation.checks.resource_limits.no import NoOpResourceProbe


NO_OPS = (
    NoOpFilenameValidator, NoOpIdentityProbe, NoOpContentTypeDetector,
    NoOpResourceProbe, NoOpActiveContentProbe, NoOpCapabilityProbe,
)
FORMAT_NO_OPS = (NoOpResourceProbe, NoOpActiveContentProbe, NoOpCapabilityProbe)


@pytest.mark.parametrize("factory", NO_OPS)
def test_name_is_stable_across_instances_and_read_only(factory):
    first, second = factory(), factory()
    assert isinstance(first.name, str) and first.name.strip()
    assert first.name == second.name
    with pytest.raises(AttributeError):
        first.name = "changed"
    assert first.name == second.name


def test_no_op_names_are_distinct():
    assert len({factory().name for factory in NO_OPS}) == len(NO_OPS)


@pytest.mark.parametrize("factory", FORMAT_NO_OPS)
def test_format_no_op_advertises_no_inspection_coverage(factory):
    worker = factory()
    assert isinstance(worker.handles, frozenset)
    assert worker.handles == frozenset()
    with pytest.raises(AttributeError):
        worker.handles = frozenset({"pdf"})
    assert worker.handles == frozenset()
