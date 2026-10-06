"""Policy constraints and defaults, independent of worker parsing behavior."""

from itertools import product

from pydantic import ValidationError
import pytest

from raggae.documents.validation import (
    ActiveContentPolicy, ArchiveLimits, CapabilityPolicy, ContentTypePolicy,
    EncryptionPolicy, FilenamePolicy, IdentityPolicy, ImageLimits, PdfLimits,
    Severity, TextLimits,
)
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding
from raggae.documents.validation.checks.capability.policy import CapabilityFinding
from raggae.documents.validation.checks.content_type.policy import ContentTypeFinding
from raggae.documents.validation.checks.encryption.policy import EncryptionFinding
from raggae.documents.validation.checks.filename.policy import FilenameFinding
from raggae.documents.validation.checks.identity.policy import IdentityFinding
from raggae.documents.validation.checks.resource_limits.policy import (
    ResourceFinding, _default_resource_severities,
)
from raggae.documents.validation.shared import DANGEROUS_EXTENSIONS, SUPPORTED_EXTENSIONS, SUPPORTED_FORMATS


def weights(prefix, *, reject=(), warning=(), info=()):
    """Build independent expected maps from explicit finding names."""
    return {
        f"{prefix}.{name}": severity
        for severity, names in ((Severity.REJECT, reject), (Severity.WARNING, warning), (Severity.INFO, info))
        for name in names
    }


RESOURCE_WEIGHTS = weights(
    "resource_limits",
    reject=("unreadable", "too_many_pages", "page_too_large", "image_too_large",
            "too_many_entries", "expands_too_large", "expansion_ratio_too_high",
            "too_many_pixels", "too_many_frames", "nesting_too_deep"),
    info=("not_applicable",),
)

POLICY_CASES = [
    (FilenamePolicy, FilenameFinding, weights(
        "filename",
        reject=("empty", "input_too_long", "encoded_separator", "control_character",
                "hidden_character", "invalid_basename", "name_too_long", "name_too_many_bytes",
                "extension_not_allowed", "dangerous_extension"),
        warning=("path_components", "extension_missing"),
        info=("normalized", "whitespace_trimmed"),
    )),
    (IdentityPolicy, IdentityFinding, weights(
        "identity", reject=("not_found", "not_a_regular_file", "symlink", "unreadable",
                            "too_large", "too_small", "changed_while_reading"),
    )),
    (ContentTypePolicy, ContentTypeFinding, weights(
        "content_type", reject=("unreadable", "undetermined", "not_allowed"),
        warning=("extension_mismatch",), info=("subtype_assumed",),
    )),
    (EncryptionPolicy, EncryptionFinding, weights(
        "encryption", reject=("unreadable", "unsupported", "password_required",
                              "password_incorrect", "password_protected"),
        warning=("weak_encryption", "extraction_not_permitted"), info=("not_applicable",),
    )),
    (PdfLimits, ResourceFinding, RESOURCE_WEIGHTS),
    (ArchiveLimits, ResourceFinding, RESOURCE_WEIGHTS),
    (ImageLimits, ResourceFinding, RESOURCE_WEIGHTS),
    (TextLimits, ResourceFinding, RESOURCE_WEIGHTS),
    (ActiveContentPolicy, ActiveContentFinding, weights(
        "active_content", reject=("unreadable", "launch_action"),
        warning=("javascript", "auto_action", "embedded_file", "rich_media", "macro",
                 "remote_template", "script", "embedded_frame", "event_handler", "spreadsheet_formula"),
        info=("remote_reference", "not_applicable"),
    )),
    (CapabilityPolicy, CapabilityFinding, weights(
        "capability", warning=("unreadable", "empty_document"), info=("not_applicable",),
    )),
]
POLICY_CLASSES = [case[0] for case in POLICY_CASES]


@pytest.mark.parametrize("policy_class,finding_enum,expected", POLICY_CASES, ids=[cls.__name__ for cls in POLICY_CLASSES])
def test_all_finding_codes_have_the_expected_default_severity(policy_class, finding_enum, expected):
    actual = policy_class().severities
    assert set(actual) == {finding.value for finding in finding_enum}
    assert actual == expected
    assert all(isinstance(value, Severity) for value in actual.values())


@pytest.mark.parametrize("policy_class", POLICY_CLASSES, ids=lambda cls: cls.__name__)
def test_policy_default_maps_are_independent(policy_class):
    first, second = policy_class(), policy_class()
    original = dict(second.severities)
    assert first.severities is not second.severities
    first.severities["test.custom"] = Severity.INFO
    assert second.severities == original
    assert policy_class().severities == original


@pytest.mark.parametrize("policy_class", POLICY_CLASSES, ids=lambda cls: cls.__name__)
def test_policy_attributes_are_frozen(policy_class):
    with pytest.raises(ValidationError) as error:
        policy_class().severities = {}
    assert error.value.errors()[0]["type"] == "frozen_instance"


@pytest.mark.parametrize("policy_class", POLICY_CLASSES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("severity", ["info", "warning", "reject"])
def test_custom_policy_severity_map_replaces_defaults_and_converts_values(policy_class, severity):
    supplied = {"custom.finding": severity}
    policy = policy_class(severities=supplied)
    assert policy.severities == {"custom.finding": Severity(severity)}
    assert policy.severities["custom.finding"] is Severity(severity)
    supplied["custom.finding"] = "info" if severity != "info" else "reject"
    assert policy.severities["custom.finding"] is Severity(severity)
    assert policy_class(severities={}).severities == {}


@pytest.mark.parametrize("policy_class", POLICY_CLASSES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("invalid", [None, {"test.custom": "fatal"}])
def test_invalid_policy_severity_maps_fail_validation(policy_class, invalid):
    with pytest.raises(ValidationError):
        policy_class(severities=invalid)


@pytest.mark.parametrize("policy_class", POLICY_CLASSES, ids=lambda cls: cls.__name__)
def test_default_policies_round_trip_through_json(policy_class):
    original = policy_class()
    assert policy_class.model_validate_json(original.model_dump_json()) == original


DEFAULTS = [
    (FilenamePolicy, dict(max_submitted_chars=1024, max_name_chars=200, max_name_utf8_bytes=512,
                         require_extension=True, reject_path_components=True,
                         allowed_extensions=SUPPORTED_EXTENSIONS, dangerous_extensions=DANGEROUS_EXTENSIONS)),
    (IdentityPolicy, dict(max_bytes=100_000_000, min_bytes=1, allow_symlinks=False,
                         compute_digest=True, hash_algorithm="sha256", read_chunk_bytes=1_048_576)),
    (ContentTypePolicy, dict(prefix_bytes=8192, allowed_formats=SUPPORTED_FORMATS,
                            trust_extension_for_text=True, require_known_format=True)),
    (EncryptionPolicy, dict(accept_password_protected=True,
                           recoverable=frozenset({"encryption.password_required", "encryption.password_incorrect"}))),
    (PdfLimits, dict(max_pages=2000, max_page_points=20_000, max_image_pixels=100_000_000)),
    (ArchiveLimits, dict(max_entries=1000, max_uncompressed_bytes=1_000_000_000, max_expansion_ratio=100.0)),
    (ImageLimits, dict(max_pixels=100_000_000, max_frames=100)),
    (TextLimits, dict(max_nesting_depth=100)),
    (ActiveContentPolicy, dict(allowed_reference_hosts=frozenset())),
    (CapabilityPolicy, dict(sampled_pages=3, min_characters_per_page=20)),
]


@pytest.mark.parametrize("policy_class,expected", DEFAULTS, ids=[cls.__name__ for cls, _ in DEFAULTS])
def test_policy_configuration_defaults(policy_class, expected):
    assert policy_class().model_dump(exclude={"severities"}) == expected


# Each declared numeric constraint is exercised independently of other defaults.
NUMERIC_FIELDS = [
    (FilenamePolicy, "max_submitted_chars", 1, {}),
    (FilenamePolicy, "max_name_chars", 1, {}),
    (FilenamePolicy, "max_name_utf8_bytes", 1, {}),
    (IdentityPolicy, "max_bytes", 1, {"min_bytes": 0}),
    (IdentityPolicy, "min_bytes", 0, {}),
    (IdentityPolicy, "read_chunk_bytes", 1, {}),
    (ContentTypePolicy, "prefix_bytes", 16, {}),
    (PdfLimits, "max_pages", 1, {}),
    (PdfLimits, "max_page_points", 0.5, {}),
    (PdfLimits, "max_image_pixels", 1, {}),
    (ArchiveLimits, "max_entries", 1, {}),
    (ArchiveLimits, "max_uncompressed_bytes", 1, {}),
    (ArchiveLimits, "max_expansion_ratio", 1.01, {}),
    (ImageLimits, "max_pixels", 1, {}),
    (ImageLimits, "max_frames", 1, {}),
    (TextLimits, "max_nesting_depth", 1, {}),
    (CapabilityPolicy, "sampled_pages", 1, {}),
    (CapabilityPolicy, "min_characters_per_page", 0, {}),
]


@pytest.mark.parametrize("policy_class,field,valid,extra", NUMERIC_FIELDS,
                         ids=[f"{cls.__name__}-{field}" for cls, field, _, _ in NUMERIC_FIELDS])
@pytest.mark.parametrize("offset", [0, 1, 2])
def test_numeric_policy_fields_accept_boundary_and_custom_values(policy_class, field, valid, extra, offset):
    value = valid + offset
    assert getattr(policy_class(**{**extra, field: value}), field) == value


@pytest.mark.parametrize("policy_class,field,valid,extra", NUMERIC_FIELDS,
                         ids=[f"{cls.__name__}-{field}" for cls, field, _, _ in NUMERIC_FIELDS])
@pytest.mark.parametrize("invalid_kind", ["below", "negative", "none"])
def test_numeric_policy_fields_reject_out_of_range_values(policy_class, field, valid, extra, invalid_kind):
    invalid = {"below": valid - 1, "negative": -1, "none": None}[invalid_kind]
    # Float limits use strict lower bounds, not a one-unit minimum.
    if field == "max_page_points" and invalid_kind == "below":
        invalid = 0
    if field == "max_expansion_ratio" and invalid_kind == "below":
        invalid = 1
    with pytest.raises(ValidationError) as error:
        policy_class(**{**extra, field: invalid})
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("require_extension,reject_paths", tuple(product([False, True], repeat=2)))
def test_filename_boolean_combinations(require_extension, reject_paths):
    policy = FilenamePolicy(require_extension=require_extension, reject_path_components=reject_paths)
    assert policy.require_extension is require_extension
    assert policy.reject_path_components is reject_paths


def test_filename_extension_sets_normalize_and_deduplicate():
    policy = FilenamePolicy(
        allowed_extensions={" .PDF ", "pdf", "TXT", " ", ""},
        dangerous_extensions={" .EXE ", "exe", "SH", " "},
    )
    assert policy.allowed_extensions == {"pdf", "txt"}
    assert policy.dangerous_extensions == {"exe", "sh"}
    empty = FilenamePolicy(allowed_extensions=set(), dangerous_extensions=set())
    assert not empty.allowed_extensions
    assert not empty.dangerous_extensions


def test_filename_rejects_normalized_allowlist_dangerous_overlap():
    with pytest.raises(ValidationError, match="both allowed and dangerous: exe, sh"):
        FilenamePolicy(allowed_extensions={" .EXE ", "SH"}, dangerous_extensions={"exe", ".sh"})


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b", "blake2s"])
def test_identity_hash_algorithms_normalize(algorithm):
    assert IdentityPolicy(hash_algorithm=f" {algorithm.upper()} ").hash_algorithm == algorithm


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "unknown", "", " "])
def test_identity_rejects_unapproved_hash_algorithms(algorithm):
    with pytest.raises(ValidationError, match="Unsupported hash algorithm"):
        IdentityPolicy(hash_algorithm=algorithm)


@pytest.mark.parametrize("allow_symlinks,compute_digest", tuple(product([False, True], repeat=2)))
def test_identity_boolean_combinations(allow_symlinks, compute_digest):
    policy = IdentityPolicy(allow_symlinks=allow_symlinks, compute_digest=compute_digest)
    assert policy.allow_symlinks is allow_symlinks
    assert policy.compute_digest is compute_digest


def test_identity_minimum_may_equal_maximum_or_be_zero():
    assert IdentityPolicy(min_bytes=5, max_bytes=5).min_bytes == 5
    assert IdentityPolicy(min_bytes=0, max_bytes=1).min_bytes == 0
    with pytest.raises(ValidationError, match="cannot exceed"):
        IdentityPolicy(min_bytes=6, max_bytes=5)


def test_content_type_format_sets_normalize_and_can_be_empty():
    assert ContentTypePolicy(allowed_formats={" .PDF ", "pdf", "TXT", " "}).allowed_formats == {"pdf", "txt"}
    assert ContentTypePolicy(allowed_formats=set()).allowed_formats == frozenset()


@pytest.mark.parametrize("format_name", ["jpg", "tif", "zip", "exe", "unknown"])
def test_content_type_allowlist_requires_canonical_supported_formats(format_name):
    with pytest.raises(ValidationError, match="Not canonical format names"):
        ContentTypePolicy(allowed_formats={format_name})


@pytest.mark.parametrize("trust_extension,require_known", tuple(product([False, True], repeat=2)))
def test_content_type_boolean_combinations(trust_extension, require_known):
    policy = ContentTypePolicy(trust_extension_for_text=trust_extension, require_known_format=require_known)
    assert policy.trust_extension_for_text is trust_extension
    assert policy.require_known_format is require_known


@pytest.mark.parametrize("accept", [False, True])
def test_encryption_acceptance_configuration_and_recoverable_codes(accept):
    policy = EncryptionPolicy(accept_password_protected=accept)
    assert policy.accept_password_protected is accept
    assert policy.recoverable == {"encryption.password_required", "encryption.password_incorrect"}
    assert "encryption.password_protected" not in policy.recoverable
    assert EncryptionPolicy(recoverable=set()).recoverable == frozenset()


def test_active_content_allowlist_normalizes_host_names():
    policy = ActiveContentPolicy(allowed_reference_hosts={" .EXAMPLE.COM. ", "example.com", " CDN.Example.COM ", " ", "..."})
    assert policy.allowed_reference_hosts == {"example.com", "cdn.example.com"}
    assert ActiveContentPolicy(allowed_reference_hosts=set()).allowed_reference_hosts == frozenset()


def test_resource_severity_factory_is_complete_and_independent():
    first, second = _default_resource_severities(), _default_resource_severities()
    assert first == second == RESOURCE_WEIGHTS
    assert first is not second
    first["resource_limits.unreadable"] = Severity.INFO
    assert second["resource_limits.unreadable"] is Severity.REJECT
    assert _default_resource_severities() == RESOURCE_WEIGHTS
