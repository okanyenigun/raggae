from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.validation import EncryptionPolicy, PdfEncryptionProbe
from raggae.documents.validation.checks.encryption import pdf as encryption_module
from raggae.documents.validation.checks.encryption.policy import EncryptionFinding as Code


USER_PASSWORD = "reader secret"
OWNER_PASSWORD = "owner secret"


class EncryptionDocument:
    def __init__(self, *, encrypted=True, revision=6, bits=256, extraction=True):
        self.is_encrypted = encrypted
        self.allow = SimpleNamespace(extract=extraction)
        self.encryption = SimpleNamespace(R=revision, bits=bits)
        self.closed = False

    def __enter__(self):
        assert not self.closed
        return self

    def __exit__(self, *exc):
        self.closed = True


@pytest.mark.parametrize("format_name", [None, "pdf", " .PDF "])
@pytest.mark.parametrize("as_string", [False, True])
def test_unencrypted_pdf_returns_access_facts_without_encryption_metadata(pdf_factory, format_name, as_string):
    path = pdf_factory()
    before = path.read_bytes()
    outcome = PdfEncryptionProbe().check(str(path) if as_string else path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {"encrypted": False, "password_required": False, "extraction_allowed": True}
    assert path.read_bytes() == before


@pytest.mark.parametrize("password", [None, "", "wrong", USER_PASSWORD, OWNER_PASSWORD])
@pytest.mark.parametrize("accept", [False, True])
def test_real_locked_pdf_password_and_policy_combinations(pdf_factory, password, accept):
    path = pdf_factory(encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD, R=6))
    before = path.read_bytes()
    outcome = PdfEncryptionProbe(EncryptionPolicy(accept_password_protected=accept)).check(path, "pdf", password)
    if password is None:
        assert outcome.codes == (Code.PASSWORD_REQUIRED,)
        assert dict(outcome.facts) == {"encrypted": True, "password_required": True}
    elif password not in {USER_PASSWORD, OWNER_PASSWORD}:
        assert outcome.codes == (Code.PASSWORD_INCORRECT,)
        assert dict(outcome.facts) == {"encrypted": True, "password_required": True}
    else:
        assert outcome.codes == (() if accept else (Code.PASSWORD_PROTECTED,))
        assert dict(outcome.facts) == {"encrypted": True, "password_required": True,
                                      "extraction_allowed": True, "encryption_bits": 256, "encryption_revision": 6}
    serialized = outcome.model_dump_json()
    assert USER_PASSWORD not in serialized
    assert OWNER_PASSWORD not in serialized
    assert path.read_bytes() == before


@pytest.mark.parametrize("password", [None, "wrong", OWNER_PASSWORD])
@pytest.mark.parametrize("accept", [False, True])
def test_empty_user_password_is_encrypted_but_not_locked(pdf_factory, password, accept):
    path = pdf_factory(encryption=pikepdf.Encryption(user="", owner=OWNER_PASSWORD, R=6))
    outcome = PdfEncryptionProbe(EncryptionPolicy(accept_password_protected=accept)).check(path, password=password)
    assert outcome.codes == ()
    assert outcome.facts["encrypted"] is True
    assert outcome.facts["password_required"] is False
    assert outcome.facts["encryption_bits"] == 256
    assert outcome.facts["encryption_revision"] == 6


@pytest.mark.parametrize("allowed", [False, True])
def test_extraction_permission_is_reported_without_enforcement(pdf_factory, allowed):
    path = pdf_factory(encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD,
                       allow=pikepdf.Permissions(extract=allowed)))
    before = path.read_bytes()
    outcome = PdfEncryptionProbe().check(path, password=USER_PASSWORD)
    assert outcome.codes == (() if allowed else (Code.EXTRACTION_NOT_PERMITTED,))
    assert outcome.facts["extraction_allowed"] is allowed
    assert outcome.facts["password_required"] is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("revision,bits,aes,metadata,weak", [
    (2, 40, False, False, True), (3, 128, False, False, True),
    (4, 128, True, True, False), (6, 256, True, True, False),
])
def test_real_encryption_revisions_and_key_metadata(pdf_factory, revision, bits, aes, metadata, weak):
    path = pdf_factory(encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD,
                       R=revision, aes=aes, metadata=metadata))
    outcome = PdfEncryptionProbe().check(path, password=USER_PASSWORD)
    assert outcome.codes == ((Code.WEAK_ENCRYPTION,) if weak else ())
    assert outcome.facts["encryption_revision"] == revision
    assert outcome.facts["encryption_bits"] == bits
    if weak:
        assert dict(outcome.findings[0].detail) == {"revision": revision, "bits": bits}


@pytest.mark.parametrize("revision", [2, 3, 4])
@pytest.mark.parametrize("bits", [39, 40, 41])
def test_revision_and_key_thresholds_are_independent(missing_path, monkeypatch, revision, bits):
    document = EncryptionDocument(revision=revision, bits=bits)
    worker = PdfEncryptionProbe()
    monkeypatch.setattr(worker, "_open", lambda path, password: document)
    outcome = worker.check(missing_path)
    assert outcome.codes == ((Code.WEAK_ENCRYPTION,) if revision <= 3 or bits <= 40 else ())
    assert outcome.facts["encryption_revision"] == revision
    assert outcome.facts["encryption_bits"] == bits
    assert document.closed


def test_combined_password_refusal_weak_encryption_and_permission_findings(missing_path, monkeypatch):
    document = EncryptionDocument(revision=3, bits=40, extraction=False)
    worker = PdfEncryptionProbe(EncryptionPolicy(accept_password_protected=False))
    calls = []

    def controlled_open(path, password):
        calls.append(password)
        if password == "":
            raise pikepdf.PasswordError("locked")
        return document

    monkeypatch.setattr(worker, "_open", controlled_open)
    outcome = worker.check(missing_path, password=USER_PASSWORD)
    assert calls == ["", USER_PASSWORD]
    assert outcome.codes == (Code.PASSWORD_PROTECTED, Code.WEAK_ENCRYPTION, Code.EXTRACTION_NOT_PERMITTED)
    assert document.closed


@pytest.mark.parametrize("setter,value", [("set_last_rc4_revision", 4), ("set_weak_key_bits", 128)])
def test_threshold_setters_are_instance_local(missing_path, monkeypatch, setter, value):
    changed, default = PdfEncryptionProbe(), PdfEncryptionProbe()
    getattr(changed, setter)(value)
    for worker in (changed, default):
        monkeypatch.setattr(worker, "_open", lambda path, password: EncryptionDocument(revision=4, bits=128))
    assert changed.check(missing_path).codes == (Code.WEAK_ENCRYPTION,)
    assert default.check(missing_path).codes == ()


@pytest.mark.parametrize("bad_input", ["missing", "corrupt"])
def test_missing_and_corrupt_pdf_are_unreadable(missing_path, file_factory, bad_input):
    path = missing_path if bad_input == "missing" else file_factory(content=b"not a PDF")
    outcome = PdfEncryptionProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert outcome.findings[0].detail["path"] == str(path)


@pytest.mark.parametrize("phase", ["initial", "retry"])
@pytest.mark.parametrize("message,code", [
    ("Unsupported encryption", Code.UNSUPPORTED), ("SCHEME NOT SUPPORTED", Code.UNSUPPORTED),
    ("damaged cross-reference", Code.UNREADABLE), ("permission denied", Code.UNREADABLE),
])
def test_open_failure_classification_on_initial_and_password_retry(missing_path, monkeypatch, phase, message, code):
    worker, calls = PdfEncryptionProbe(), []

    def failing_open(path, password):
        calls.append(password)
        if phase == "retry" and password == "":
            raise pikepdf.PasswordError("locked")
        raise ValueError(message)

    monkeypatch.setattr(worker, "_open", failing_open)
    outcome = worker.check(missing_path, password=USER_PASSWORD)
    assert calls == ([""] if phase == "initial" else ["", USER_PASSWORD])
    assert outcome.codes == (code,)
    assert outcome.findings[0].detail["path"] == str(missing_path)
    assert USER_PASSWORD not in outcome.model_dump_json()


@pytest.mark.parametrize("format_name,normalized", [("txt", "txt"), (" .JPG ", "jpeg"), ("DOCX", "docx")])
def test_non_pdf_format_does_not_open_input(missing_path, monkeypatch, forbidden_operation, format_name, normalized):
    worker = PdfEncryptionProbe()
    monkeypatch.setattr(worker, "_open", forbidden_operation)
    outcome = worker.check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("value", [None, b"file.pdf", 7, object()])
def test_invalid_path_type_is_rejected(value):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        PdfEncryptionProbe().check(value)


def test_empty_password_is_tried_first_and_opening_is_strict(missing_path, monkeypatch):
    document, calls = EncryptionDocument(), []

    def recording_open(path, **kwargs):
        calls.append((path, kwargs))
        if kwargs["password"] == "":
            raise pikepdf.PasswordError("locked")
        return document

    monkeypatch.setattr(encryption_module.pikepdf, "Pdf", SimpleNamespace(open=recording_open))
    outcome = PdfEncryptionProbe().check(missing_path, password=USER_PASSWORD)
    assert calls == [(missing_path, {"password": "", "attempt_recovery": False}),
                     (missing_path, {"password": USER_PASSWORD, "attempt_recovery": False})]
    assert outcome.codes == ()
    assert document.closed


def test_successful_empty_password_does_not_retry_supplied_password(missing_path, monkeypatch):
    document, calls = EncryptionDocument(encrypted=False), []
    policy = EncryptionPolicy()
    worker = PdfEncryptionProbe(policy)

    def recording_open(path, password):
        calls.append(password)
        return document

    monkeypatch.setattr(worker, "_open", recording_open)
    assert worker.check(missing_path, password="unnecessary").codes == ()
    assert calls == [""]
    assert document.closed
    assert worker.policy is policy
    assert worker.name == "validation_encryption_pdf"
    assert worker.handles == frozenset({"pdf"})


def test_repeated_calls_do_not_remember_password_or_prior_findings(pdf_factory):
    locked = pdf_factory("locked.pdf", encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD))
    plain = pdf_factory("plain.pdf")
    worker = PdfEncryptionProbe()
    assert worker.check(locked).codes == (Code.PASSWORD_REQUIRED,)
    assert worker.check(locked, password=USER_PASSWORD).codes == ()
    assert worker.check(locked).codes == (Code.PASSWORD_REQUIRED,)
    clean = worker.check(plain)
    assert clean.codes == ()
    assert dict(clean.facts) == {"encrypted": False, "password_required": False, "extraction_allowed": True}
