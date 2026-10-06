from pathlib import Path
import urllib.request

import pytest

from raggae.documents.validation import ActiveContentPolicy
from raggae.documents.validation.checks.active_content.base import reference_finding
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("reference,expected", [
    ("https://example.com/path", True), ("http://EXAMPLE.COM", True),
    ("https://cdn.example.com/path", True), ("https://deep.cdn.example.com", True),
    ("https://example.com:8443/path", True), ("//cdn.example.com/path", True),
    ("https://user:password@example.com/path", True),
    ("https://evil.test/", False), ("https://notexample.com/", False),
    ("https://example.com.evil.test/", False), ("https://evilexample.com/", False),
    ("https://evil.test/example.com", False), ("https://evil.test/?host=example.com", False),
    ("https://example.com@evil.test/", False),
    ("mailto:user@example.com", False), ("file:///tmp/example.com", False),
    ("javascript:alert('example.com')", False), ("example.com/path", False),
    ("", False), ("https:///example.com", False), ("https://[invalid", False),
])
def test_allowlist_uses_parsed_host_and_subdomain_boundary(reference, expected):
    policy = ActiveContentPolicy(allowed_reference_hosts={" .EXAMPLE.COM. "})
    assert policy.allows(reference) is expected


@pytest.mark.parametrize("allowed,reference", [
    ({"192.0.2.1"}, "https://192.0.2.1:8443/path"),
    ({"2001:db8::1"}, "https://[2001:db8::1]:8443/path"),
    ({"example.com", "other.test"}, "https://cdn.other.test/path"),
])
def test_multiple_hosts_and_ip_addresses(allowed, reference):
    assert ActiveContentPolicy(allowed_reference_hosts=allowed).allows(reference)


@pytest.mark.parametrize("reference", ["https://example.com", "https://cdn.example.com", "file:///tmp/file", ""])
def test_empty_allowlist_suppresses_nothing(reference):
    assert not ActiveContentPolicy().allows(reference)


def test_host_allowlist_does_not_claim_url_or_scheme_validation():
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    assert policy.allows("custom-scheme://example.com/path")
    assert policy.allows("file://example.com/shared/file")
    # URL trailing dots are not normalized by the current host-only policy.
    assert not policy.allows("https://example.com./path")


@pytest.mark.parametrize("references", [[], ["https://example.com/a"],
                                        ["https://example.com/a", "https://cdn.example.com/b"]])
def test_no_reportable_references_yield_no_finding(references):
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    assert reference_finding(references, policy) == []


def test_mixed_references_are_filtered_deduplicated_and_sorted():
    references = ["https://z.evil.test/", "https://example.com/safe", "https://a.evil.test/",
                  "https://z.evil.test/", "https://cdn.example.com/safe"]
    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    before = references[:]
    findings = reference_finding(references, policy)
    assert len(findings) == 1
    assert findings[0].code == Code.REMOTE_REFERENCE
    assert dict(findings[0].detail) == {"count": 2, "sample": "https://a.evil.test/, https://z.evil.test/"}
    assert references == before


@pytest.mark.parametrize("sample_size", [0, 1, 3, 5, 10])
def test_sampling_limits_sample_not_total_count(sample_size):
    references = [f"https://{index}.evil.test/" for index in reversed(range(7))]
    findings = reference_finding(references + references, ActiveContentPolicy(), sample_size)
    assert len(findings) == 1
    assert findings[0].code == Code.REMOTE_REFERENCE
    assert dict(findings[0].detail) == {"count": 7, "sample": ", ".join(sorted(references)[:sample_size])}


def test_default_sample_contains_five_references_and_is_deterministic():
    references = [f"https://{index}.evil.test/" for index in range(7)]
    first = reference_finding(references, ActiveContentPolicy())
    assert first == reference_finding(list(reversed(references)), ActiveContentPolicy())
    assert first[0].detail["count"] == 7
    assert first[0].detail["sample"] == ", ".join(references[:5])


def test_helpers_never_fetch_or_read_references(monkeypatch, forbidden_operation):
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    monkeypatch.setattr(Path, "read_bytes", forbidden_operation)
    monkeypatch.setattr(Path, "read_text", forbidden_operation)
    findings = reference_finding(["https://evil.test/a", "file:///tmp/private"], ActiveContentPolicy())
    assert findings[0].code == Code.REMOTE_REFERENCE
    assert findings[0].detail["count"] == 2


def test_policy_instances_and_repeated_calls_do_not_remember_references():
    allowing = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    default = ActiveContentPolicy()
    assert reference_finding(["https://example.com/a"], allowing) == []
    assert reference_finding(["https://example.com/a"], default)[0].detail["count"] == 1
    assert reference_finding([], default) == []
    assert reference_finding(["https://evil.test/a"], allowing)[0].detail["count"] == 1
