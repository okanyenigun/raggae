"""Invalid tail bytes are not merely valid characters cut by prefix reading."""

import pytest

from raggae.documents.validation import ContentTypePolicy, SignatureContentTypeDetector
from raggae.documents.validation.checks.content_type.policy import ContentTypeFinding as Code


@pytest.mark.parametrize("invalid_bytes", [b"\xff", b"\x80", b"\xc0\xaf", b"\xed\xa0\x80"],
                         ids=["invalid-start", "stray-continuation", "overlong", "encoded-surrogate"])
@pytest.mark.parametrize("require_known", [True, False])
def test_invalid_utf8_at_prefix_end_does_not_invent_a_text_format(
    file_factory, invalid_bytes, require_known,
):
    path = file_factory(content=b"plain text" + invalid_bytes)
    worker = SignatureContentTypeDetector(ContentTypePolicy(require_known_format=require_known))
    outcome = worker.check(path)
    assert dict(outcome.facts) == {}, "Invalid UTF-8 must not be reported as detected TXT."
    assert outcome.codes == ((Code.UNDETERMINED,) if require_known else ())


@pytest.mark.parametrize("invalid_bytes", [
    b"\xc2A", b"\xe0\x80", b"\xed\xa0", b"\xf0\x8f",
    b"\xf4\x90", b"\xf5", b"\xc0", b"\xc1",
])
@pytest.mark.parametrize("claim", [None, "txt", "jsonl"])
def test_invalid_partial_sequences_are_not_hidden_by_extension_trust(file_factory, invalid_bytes, claim):
    path = file_factory(content=b"plain text" + invalid_bytes)
    outcome = SignatureContentTypeDetector().check(path, claim)
    assert outcome.codes == (Code.UNDETERMINED,)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("data", [b"a" * 15 + b"\xff" + b"rest", b"a" * 14 + b"\xc2A" + b"rest"])
def test_invalid_bytes_exactly_at_read_limit_are_not_valid_truncations(file_factory, data):
    outcome = SignatureContentTypeDetector(ContentTypePolicy(prefix_bytes=16)).check(file_factory(content=data))
    assert outcome.codes == (Code.UNDETERMINED,)
    assert dict(outcome.facts) == {}
