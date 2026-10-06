from types import SimpleNamespace

import pytest

from raggae.documents.validation import (
    ArchiveResourceProbe, CsvActiveContentProbe, DecisionPolicy, DocumentInspector,
    FileIdentityProbe, HtmlActiveContentProbe, ImageResourceProbe, OoxmlActiveContentProbe,
    PdfActiveContentProbe, PdfCapabilityProbe, PdfEncryptionProbe, PdfResourceProbe,
    SignatureContentTypeDetector, StrictFilenameValidator, TextResourceProbe,
)


DEPENDENCIES = (
    "filename_validator", "identity_probe", "content_type_detector", "encryption_probe",
    "resource_probes", "active_content_probes", "capability_probes", "policy",
)
GROUPS = ("resource_probes", "active_content_probes")


class FalseyDependency(SimpleNamespace):
    def __bool__(self):
        return False


class FalseyWorkers(tuple):
    def __bool__(self):
        return False


@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_assembly_and_explicit_none(explicit_none):
    inspector = DocumentInspector(**{key: None for key in DEPENDENCIES}) if explicit_none else DocumentInspector()
    singles = {
        "filename_validator": StrictFilenameValidator,
        "identity_probe": FileIdentityProbe,
        "content_type_detector": SignatureContentTypeDetector,
        "encryption_probe": PdfEncryptionProbe,
        "capability_probes": PdfCapabilityProbe,
        "policy": DecisionPolicy,
    }
    for key, expected in singles.items():
        assert type(getattr(inspector, f"_{key}")) is expected
    assert tuple(type(worker) for worker in inspector._resource_probes) == (
        PdfResourceProbe, ArchiveResourceProbe, ImageResourceProbe, TextResourceProbe,
    )
    assert tuple(type(worker) for worker in inspector._active_content_probes) == (
        PdfActiveContentProbe, OoxmlActiveContentProbe, HtmlActiveContentProbe, CsvActiveContentProbe,
    )


@pytest.mark.parametrize("dependency", DEPENDENCIES)
def test_default_instances_do_not_share_dependencies(dependency):
    first, second = DocumentInspector(), DocumentInspector()
    left, right = getattr(first, f"_{dependency}"), getattr(second, f"_{dependency}")
    assert left is not right
    if dependency in GROUPS:
        assert all(a is not b for a, b in zip(left, right, strict=True))
    if dependency == "policy":
        assert left.severity_by_code is not right.severity_by_code
        left.severity_by_code.clear()
        assert right.severity_by_code


@pytest.mark.parametrize("dependency", DEPENDENCIES)
@pytest.mark.parametrize("falsey", [False, True])
def test_supplied_dependency_is_retained_even_when_falsey(dependency, falsey):
    supplied = FalseyDependency(name="custom") if falsey else SimpleNamespace(name="custom")
    if dependency in GROUPS:
        supplied = FalseyWorkers((supplied,)) if falsey else (supplied,)
    inspector = DocumentInspector(**{dependency: supplied})
    assert getattr(inspector, f"_{dependency}") is supplied


@pytest.mark.parametrize("dependency", GROUPS)
def test_empty_group_is_not_replaced_by_defaults(dependency):
    inspector = DocumentInspector(**{dependency: ()})
    assert getattr(inspector, f"_{dependency}") == ()


@pytest.mark.parametrize("count", range(1, 9))
def test_constructor_dependencies_are_keyword_only(count):
    with pytest.raises(TypeError):
        DocumentInspector(*([None] * count))


def test_full_custom_assembly_is_retained(inspector_factory):
    policy = DecisionPolicy(stop_on_first_rejection=False)
    assembly = inspector_factory(policy=policy)
    for dependency, stage in (
        ("filename_validator", "filename"), ("identity_probe", "identity"),
        ("content_type_detector", "content_type"), ("encryption_probe", "encryption"),
        ("capability_probes", "capability"),
    ):
        assert getattr(assembly.inspector, f"_{dependency}") is assembly.workers[stage]
    assert assembly.inspector._resource_probes == (assembly.workers["resource"],)
    assert assembly.inspector._active_content_probes == (assembly.workers["active_content"],)
    assert assembly.inspector._policy is policy
