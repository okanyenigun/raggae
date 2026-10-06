"""Filename behavior, boundaries, combinations, and safe failure cases."""

from pathlib import Path
import re

import pytest

from raggae.documents.validation import FilenamePolicy, StrictFilenameValidator
from raggae.documents.validation.checks.filename.policy import FilenameFinding as Code
from raggae.documents.validation.shared import DANGEROUS_EXTENSIONS, SUPPORTED_EXTENSIONS


@pytest.mark.parametrize("extension", sorted(SUPPORTED_EXTENSIONS))
@pytest.mark.parametrize("upper_case", [False, True])
def test_supported_extensions_are_accepted_and_claimed_casefolded(extension, upper_case):
    submitted = f"report.{extension.upper() if upper_case else extension}"
    outcome = StrictFilenameValidator().check(submitted)
    assert outcome.findings == ()
    assert dict(outcome.facts) == {
        "canonical_filename": submitted, "claimed_extension": extension,
    }


@pytest.mark.parametrize("filename", ["résumé.pdf", "my report.pdf", "report.backup.pdf", ".hidden.pdf"])
def test_safe_unicode_spaces_and_multiple_suffixes(filename):
    outcome = StrictFilenameValidator().check(filename)
    assert outcome.codes == ()
    assert outcome.facts["canonical_filename"] == filename
    assert outcome.facts["claimed_extension"] == "pdf"


@pytest.mark.parametrize("filename", [None, b"report.pdf", 1, False, Path("report.pdf"), [], object()])
def test_non_string_filename_is_rejected(filename):
    with pytest.raises(TypeError, match="submitted_filename must be a string"):
        StrictFilenameValidator().check(filename)


def test_empty_input_returns_only_empty_finding_and_no_facts():
    outcome = StrictFilenameValidator().check("")
    assert outcome.codes == (Code.EMPTY,)
    assert dict(outcome.facts) == {}


@pytest.mark.parametrize("filename,canonical", [
    (" ", ""), (".", "."), ("..", ".."), ("/", ""),
    ("\\", ""), ("folder/", ""), ("C:", ""),
])
def test_unusable_basenames_are_reported(filename, canonical):
    outcome = StrictFilenameValidator().check(filename)
    assert outcome.found(Code.INVALID_BASENAME)
    assert outcome.found(Code.EXTENSION_MISSING)
    assert not outcome.found(Code.EMPTY)
    assert outcome.facts.get("canonical_filename", "") == canonical
    assert "claimed_extension" not in outcome.facts


@pytest.mark.parametrize("submitted", [
    "/uploads/report.pdf", "../report.pdf", "../../report.pdf",
    r"C:\uploads\report.pdf", "C:report.pdf", r"\\server\share\report.pdf",
    r"folder\child/report.pdf", "Z:/folder/report.pdf",
])
def test_directory_components_are_stripped_and_reported(submitted):
    outcome = StrictFilenameValidator().check(submitted)
    assert outcome.codes == (Code.PATH_COMPONENTS,)
    assert dict(outcome.facts) == {"canonical_filename": "report.pdf", "claimed_extension": "pdf"}


@pytest.mark.parametrize("require_extension", [False, True])
@pytest.mark.parametrize("reject_paths", [False, True])
def test_path_and_extension_policy_combinations(require_extension, reject_paths):
    worker = StrictFilenameValidator(FilenamePolicy(
        require_extension=require_extension, reject_path_components=reject_paths,
    ))
    expected = tuple(code for enabled, code in [
        (reject_paths, Code.PATH_COMPONENTS), (require_extension, Code.EXTENSION_MISSING),
    ] if enabled)
    outcome = worker.check("../report")
    assert outcome.codes == expected
    assert dict(outcome.facts) == {"canonical_filename": "report"}


def test_path_stripping_preserves_dangerous_and_disallowed_extension_findings():
    outcome = StrictFilenameValidator().check("../app/config.py")
    assert outcome.codes == (Code.PATH_COMPONENTS, Code.DANGEROUS_EXTENSION, Code.EXTENSION_NOT_ALLOWED)
    assert dict(outcome.facts) == {"canonical_filename": "config.py", "claimed_extension": "py"}


@pytest.mark.parametrize("sequence", ["%00", "%0a", "%0A", "%0d", "%0D", "%2f", "%2F", "%5c", "%5C"])
def test_encoded_separators_and_controls_are_reported(sequence):
    outcome = StrictFilenameValidator().check(f"report{sequence}.pdf")
    assert outcome.codes == (Code.ENCODED_SEPARATOR,)
    assert dict(outcome.findings[0].detail) == {"sequence": sequence}


def test_multiple_encoded_sequences_report_first_match_once():
    outcome = StrictFilenameValidator().check("report%2F%00%5c.pdf")
    assert outcome.codes == (Code.ENCODED_SEPARATOR,)
    assert outcome.findings[0].detail["sequence"] == "%2F"


@pytest.mark.parametrize("filename", ["report%20.pdf", "report%41.pdf", "100%.pdf"])
def test_ordinary_percent_text_is_not_an_encoded_separator(filename):
    assert StrictFilenameValidator().check(filename).codes == ()


@pytest.mark.parametrize("character", ["\x00", "\n", "\r", "\t", "\x1f", "\x7f"])
def test_control_characters_are_findings_not_silent_cleanup(character):
    assert StrictFilenameValidator().check(f"re{character}port.pdf").codes == (Code.CONTROL_CHARACTER,)


@pytest.mark.parametrize("character", [
    "\u00ad", "\u061c", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2060", "\u2061",
    "\u2062", "\u2063", "\u2064", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff",
])
def test_hidden_and_directional_characters_are_reported(character):
    assert StrictFilenameValidator().check(f"re{character}port.pdf").codes == (Code.HIDDEN_CHARACTER,)


@pytest.mark.parametrize("characters,expected", [
    ("\x00\x01", Code.CONTROL_CHARACTER),
    ("\u200b\u202e", Code.HIDDEN_CHARACTER),
    ("\x00\u200b", Code.CONTROL_CHARACTER),
    ("\u200b\x00", Code.HIDDEN_CHARACTER),
])
def test_disguised_character_scan_reports_first_category_once(characters, expected):
    assert StrictFilenameValidator().check(f"re{characters}port.pdf").codes == (expected,)


@pytest.mark.parametrize("submitted,canonical,codes", [
    ("cafe\u0301.pdf", "café.pdf", (Code.NORMALIZED,)),
    (" report.pdf ", "report.pdf", (Code.WHITESPACE_TRIMMED,)),
    (" cafe\u0301.pdf ", "café.pdf", (Code.NORMALIZED, Code.WHITESPACE_TRIMMED)),
    ("café.pdf", "café.pdf", ()),
])
def test_canonicalization_reports_only_actual_changes(submitted, canonical, codes):
    outcome = StrictFilenameValidator().check(submitted)
    assert outcome.codes == codes
    assert dict(outcome.facts) == {"canonical_filename": canonical, "claimed_extension": "pdf"}


@pytest.mark.parametrize("field,code", [
    ("max_submitted_chars", Code.INPUT_TOO_LONG),
    ("max_name_chars", Code.NAME_TOO_LONG),
    ("max_name_utf8_bytes", Code.NAME_TOO_MANY_BYTES),
])
@pytest.mark.parametrize("length", [9, 10, 11])
def test_independent_length_boundaries(field, code, length):
    worker = StrictFilenameValidator(FilenamePolicy(**{field: 10}))
    outcome = worker.check("a" * (length - 4) + ".pdf")
    assert outcome.codes == ((code,) if length > 10 else ())
    if length > 10:
        assert dict(outcome.findings[0].detail) == {"limit": 10, "observed": length}


def test_raw_path_length_is_distinct_from_canonical_name_length():
    outcome = StrictFilenameValidator(FilenamePolicy(max_submitted_chars=10, max_name_chars=10)).check("folder/report.pdf")
    assert outcome.codes == (Code.INPUT_TOO_LONG, Code.PATH_COMPONENTS)
    assert dict(outcome.findings[0].detail) == {"limit": 10, "observed": len("folder/report.pdf")}
    assert outcome.facts["canonical_filename"] == "report.pdf"


@pytest.mark.parametrize("limit", [11, 12, 13])
def test_multibyte_filename_can_exceed_byte_limit_without_character_failure(limit):
    # Four é characters plus .pdf: eight characters but twelve UTF-8 bytes.
    outcome = StrictFilenameValidator(FilenamePolicy(max_name_chars=8, max_name_utf8_bytes=limit)).check("éééé.pdf")
    assert outcome.codes == ((Code.NAME_TOO_MANY_BYTES,) if limit < 12 else ())
    if limit < 12:
        assert dict(outcome.findings[0].detail) == {"limit": limit, "observed": 12}


@pytest.mark.parametrize("extension", sorted(DANGEROUS_EXTENSIONS))
def test_every_dangerous_extension_is_rejected_inside_an_allowed_suffix(extension):
    outcome = StrictFilenameValidator().check(f"report.{extension}.pdf")
    assert outcome.codes == (Code.DANGEROUS_EXTENSION,)
    assert outcome.findings[0].detail["extensions"] == extension
    assert outcome.facts["claimed_extension"] == "pdf"


@pytest.mark.parametrize("filename,claimed,codes", [
    ("report.exe", "exe", (Code.DANGEROUS_EXTENSION, Code.EXTENSION_NOT_ALLOWED)),
    ("report.unknown", "unknown", (Code.EXTENSION_NOT_ALLOWED,)),
    ("report.exe.pdf", "pdf", (Code.DANGEROUS_EXTENSION,)),
])
def test_final_extension_and_dangerous_suffix_findings_are_separate(filename, claimed, codes):
    outcome = StrictFilenameValidator().check(filename)
    assert outcome.codes == codes
    assert outcome.facts["claimed_extension"] == claimed
    if Code.EXTENSION_NOT_ALLOWED in codes:
        assert outcome.findings[-1].detail["extension"] == claimed


def test_dangerous_suffix_details_are_sorted_and_deduplicated():
    outcome = StrictFilenameValidator().check("report.SH.exe.SH.pdf")
    assert outcome.codes == (Code.DANGEROUS_EXTENSION,)
    assert outcome.findings[0].detail["extensions"] == "exe, sh"


@pytest.mark.parametrize("filename,expected", [
    ("report", ()), (".hidden", ()), ("report.", ()), (".", ()), ("..", ()),
    ("report.PDF", ("pdf",)), ("report..pdf", ("pdf",)),
    ("report.exe.pdf", ("exe", "pdf")), (".hidden.pdf", ("pdf",)),
    ("report.pdf. ", ("pdf",)),
])
def test_suffix_parser_handles_dotfiles_and_trailing_dots_or_spaces(filename, expected):
    assert StrictFilenameValidator.suffixes(filename) == expected


@pytest.mark.parametrize("filename", ["report", ".hidden"])
@pytest.mark.parametrize("require_extension", [False, True])
def test_missing_extension_policy_and_absent_claim_fact(filename, require_extension):
    outcome = StrictFilenameValidator(FilenamePolicy(require_extension=require_extension)).check(filename)
    assert outcome.codes == ((Code.EXTENSION_MISSING,) if require_extension else ())
    assert dict(outcome.facts) == {"canonical_filename": filename}


def test_combined_issues_are_collected_in_order_with_canonical_facts():
    worker = StrictFilenameValidator(FilenamePolicy(max_submitted_chars=8, max_name_chars=8, max_name_utf8_bytes=8))
    outcome = worker.check("../ e\u0301\u200b%2f.exe.pdf ")
    assert outcome.codes == (
        Code.INPUT_TOO_LONG, Code.ENCODED_SEPARATOR, Code.HIDDEN_CHARACTER,
        Code.PATH_COMPONENTS, Code.NORMALIZED, Code.WHITESPACE_TRIMMED,
        Code.NAME_TOO_LONG, Code.NAME_TOO_MANY_BYTES, Code.DANGEROUS_EXTENSION,
    )
    assert dict(outcome.facts) == {"canonical_filename": "é\u200b%2f.exe.pdf", "claimed_extension": "pdf"}


def test_settings_are_instance_local_and_policy_is_retained():
    policy = FilenamePolicy()
    changed, default = StrictFilenameValidator(policy), StrictFilenameValidator()
    assert changed.policy is policy
    assert changed.name == "validator_filename_strict"
    changed.set_encoded_separator_pattern(r"%41")
    changed.set_hidden_codepoints("$")
    changed.set_drive_prefix_pattern(r"^CUSTOM:")
    assert changed.check("report%41.pdf").found(Code.ENCODED_SEPARATOR)
    assert not default.check("report%41.pdf").found(Code.ENCODED_SEPARATOR)
    assert changed.check("re$port.pdf").found(Code.HIDDEN_CHARACTER)
    assert not default.check("re$port.pdf").found(Code.HIDDEN_CHARACTER)
    assert not changed.check("re\u200bport.pdf").found(Code.HIDDEN_CHARACTER)
    assert default.check("re\u200bport.pdf").found(Code.HIDDEN_CHARACTER)
    assert changed.check("CUSTOM:report.pdf").facts["canonical_filename"] == "report.pdf"
    assert default.check("CUSTOM:report.pdf").facts["canonical_filename"] == "CUSTOM:report.pdf"


@pytest.mark.parametrize("setter,submitted,expected", [
    ("set_encoded_separator_pattern", "report%41.pdf", Code.ENCODED_SEPARATOR),
    ("set_drive_prefix_pattern", "CUSTOM:report.pdf", Code.PATH_COMPONENTS),
])
def test_invalid_regex_does_not_replace_previous_setting(setter, submitted, expected):
    worker = StrictFilenameValidator()
    getattr(worker, setter)(r"%41" if setter == "set_encoded_separator_pattern" else r"^CUSTOM:")
    with pytest.raises(re.error):
        getattr(worker, setter)("[")
    assert worker.check(submitted).found(expected)


def test_filename_validation_does_not_access_or_modify_a_real_file(file_factory, monkeypatch, forbidden_operation):
    path = file_factory("existing.pdf", b"unchanged bytes")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden_operation)
        patch.setattr(Path, "stat", forbidden_operation)
        patch.setattr("builtins.open", forbidden_operation)
        outcome = StrictFilenameValidator().check(str(path))
    assert outcome.found(Code.PATH_COMPONENTS)
    assert outcome.facts["canonical_filename"] == path.name
    assert path.read_bytes() == b"unchanged bytes"


def test_repeated_calls_do_not_leak_findings_or_facts():
    worker = StrictFilenameValidator()
    assert worker.check("report.exe").found(Code.DANGEROUS_EXTENSION)
    clean = worker.check("report.pdf")
    assert clean.codes == ()
    assert dict(clean.facts) == {"canonical_filename": "report.pdf", "claimed_extension": "pdf"}
    assert worker.check("").facts == {}


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high-surrogate", "low-surrogate"])
def test_lone_surrogate_is_reported_without_crashing(surrogate):
    """Invalid Unicode should yield a control finding, not abort validation."""
    outcome = StrictFilenameValidator().check(f"re{surrogate}port.pdf")
    assert outcome.found(Code.CONTROL_CHARACTER)


@pytest.mark.parametrize("characters", ["\ud800", "\udfff", "\ud800\udfff", "\u200b\ud800", "\x00\ud800"])
def test_invalid_unicode_is_preserved_and_control_finding_is_not_duplicated(characters):
    submitted = f"re{characters}port.exe.pdf"
    outcome = StrictFilenameValidator(FilenamePolicy(max_name_chars=5)).check(submitted)
    assert outcome.codes.count(Code.CONTROL_CHARACTER) == 1
    assert outcome.found(Code.NAME_TOO_LONG)
    assert outcome.found(Code.DANGEROUS_EXTENSION)
    assert not outcome.found(Code.NAME_TOO_MANY_BYTES)
    assert dict(outcome.facts) == {"canonical_filename": submitted, "claimed_extension": "pdf"}
