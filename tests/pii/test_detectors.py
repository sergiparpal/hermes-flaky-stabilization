"""Phase 2 (TDD) — detectors + validators, written before detectors.py exists.

Covers, per detector: positive cases, negative cases, and the validator logic
(Luhn, IBAN mod-97, DNI/NIE control letter, SSN/ITIN structural rules). Also
asserts that masking never reveals the full raw value.
"""
import detectors
import pytest

# ── helpers ─────────────────────────────────────────────────────────────────

def _types(findings):
    return sorted(f.type for f in findings)


def _run(text, types=None):
    return detectors.run_detectors(text, types)


# ── email ─────────────────────────────────────────────────────────────────

def test_email_positive():
    hits = detectors.find_email("contact alice@example.com or bob.smith+tag@sub.domain.co.uk")
    raws = sorted(m.raw for m in hits)
    assert raws == ["alice@example.com", "bob.smith+tag@sub.domain.co.uk"]
    assert all(m.type == "email" for m in hits)


def test_email_negative():
    assert detectors.find_email("no address here: @nowhere, a@b, plain text") == []


# ── credit_card / Luhn ──────────────────────────────────────────────────────

def test_luhn_checksum():
    assert detectors.luhn_ok("4111111111111111") is True
    assert detectors.luhn_ok("4242424242424242") is True
    assert detectors.luhn_ok("5555555555554444") is True
    assert detectors.luhn_ok("4111111111111112") is False  # last digit broken


def test_credit_card_positive_and_separators():
    for text, digits in [
        ("card 4111 1111 1111 1111 on file", "4111111111111111"),
        ("card 4111-1111-1111-1111 on file", "4111111111111111"),
        ("card 5555555555554444.", "5555555555554444"),
    ]:
        hits = detectors.find_credit_card(text)
        assert len(hits) == 1, text
        assert hits[0].type == "credit_card"
        assert hits[0].raw.replace(" ", "").replace("-", "") == digits


def test_credit_card_negative_luhn_fail():
    # 16 digits but Luhn-invalid → not a card.
    assert detectors.find_credit_card("num 4111 1111 1111 1112 nope") == []


def test_credit_card_negative_too_short():
    assert detectors.find_credit_card("only 12345678 here") == []


# ── iban / mod-97 ───────────────────────────────────────────────────────────

def test_iban_mod97():
    assert detectors.iban_ok("GB82WEST12345698765432") is True
    assert detectors.iban_ok("DE89370400440532013000") is True
    assert detectors.iban_ok("ES9121000418450200051332") is True
    assert detectors.iban_ok("GB82WEST12345698765433") is False  # broken check


def test_iban_positive_with_spaces():
    hits = detectors.find_iban("wire to GB82 WEST 1234 5698 7654 32 today")
    assert len(hits) == 1
    assert hits[0].type == "iban"
    assert hits[0].raw.replace(" ", "") == "GB82WEST12345698765432"


def test_iban_negative():
    assert detectors.find_iban("ES0000000000000000000000 is bogus") == []


# ── spanish_dni_nie / mod-23 control letter ──────────────────────────────────

def test_dni_control_letter():
    assert detectors.dni_nie_ok("12345678Z") is True
    assert detectors.dni_nie_ok("12345678A") is False


def test_nie_control_letter():
    assert detectors.dni_nie_ok("X1234567L") is True
    assert detectors.dni_nie_ok("X1234567M") is False


def test_dni_nie_positive():
    assert detectors.find_spanish_dni_nie("DNI 12345678Z issued")[0].raw == "12345678Z"
    assert detectors.find_spanish_dni_nie("NIE: X1234567L")[0].raw == "X1234567L"


def test_dni_nie_negative_bad_letter():
    assert detectors.find_spanish_dni_nie("id 12345678A invalid") == []


# ── us_ssn ──────────────────────────────────────────────────────────────────

def test_ssn_positive():
    hits = detectors.find_us_ssn("SSN 123-45-6789 on record")
    assert len(hits) == 1 and hits[0].type == "us_ssn"
    assert hits[0].raw == "123-45-6789"


def test_ssn_negative_invalid_blocks():
    for bad in ["000-45-6789", "666-45-6789", "123-00-6789", "123-45-0000"]:
        assert detectors.find_us_ssn(f"id {bad} here") == [], bad


def test_ssn_negative_area_900_is_not_ssn():
    # 9xx area is ITIN territory, never a valid SSN.
    assert detectors.find_us_ssn("id 900-70-1234 here") == []


# ── us_itin ─────────────────────────────────────────────────────────────────

def test_itin_positive():
    hits = detectors.find_us_itin("ITIN 900-70-1234 filed")
    assert len(hits) == 1 and hits[0].type == "us_itin"
    assert hits[0].raw == "900-70-1234"


def test_itin_negative_group_out_of_range():
    # first digit 9 but group 69 is outside every valid ITIN range.
    assert detectors.find_us_itin("id 900-69-1234 here") == []


def test_itin_negative_not_nine_prefix():
    assert detectors.find_us_itin("id 123-70-1234 here") == []


# ── phone (conservative) ─────────────────────────────────────────────────────

def test_phone_positive_e164_and_grouped():
    assert detectors.find_phone("call +34600123456 now")[0].type == "phone"
    assert detectors.find_phone("call +44 20 7946 0958")[0].raw.strip() == "+44 20 7946 0958"
    assert detectors.find_phone("US 415-555-0132 line")[0].raw == "415-555-0132"


def test_phone_negative_plain_digit_run():
    # 10 bare digits with no '+' and no separators → too ambiguous, not matched.
    assert detectors.find_phone("order 4155550132 shipped") == []


def test_phone_negative_too_short():
    assert detectors.find_phone("ext 12345") == []


# ── passport (context-gated) ─────────────────────────────────────────────────

def test_passport_requires_context_keyword():
    hits = detectors.find_passport("Passport: X1234567 issued 2020")
    assert len(hits) == 1 and hits[0].type == "passport"
    assert hits[0].raw == "X1234567"
    assert detectors.find_passport("passport no. 123456789")[0].raw == "123456789"


def test_passport_no_keyword_no_match():
    # Same token shape, but no passport-family keyword nearby → no match.
    assert detectors.find_passport("token X1234567 unrelated") == []


def test_passport_keyword_without_number_no_match():
    assert detectors.find_passport("visit the passport office today") == []


# ── run_detectors orchestration + overlap dedup ───────────────────────────────

def test_run_detectors_default_all():
    text = "mail alice@example.com card 4111 1111 1111 1111 dni 12345678Z"
    assert _types(_run(text)) == ["credit_card", "email", "spanish_dni_nie"]


def test_run_detectors_type_subset():
    text = "mail alice@example.com card 4111 1111 1111 1111"
    assert _types(_run(text, types=["email"])) == ["email"]


def test_run_detectors_dedupes_ssn_vs_phone():
    # 123-45-6789 is a valid SSN *and* matches the loose phone shape;
    # the higher-priority SSN wins, so it is reported exactly once.
    findings = _run("record 123-45-6789 end")
    assert _types(findings) == ["us_ssn"]


def test_run_detectors_returns_findings_without_raw():
    findings = _run("mail alice@example.com")
    f = findings[0]
    assert not hasattr(f, "raw")
    assert f.preview and "alice@example.com" not in f.preview


# ── masking never reveals the full value ─────────────────────────────────────

def test_masking_never_reveals_full_value():
    samples = {
        "email": "alice@example.com",
        "credit_card": "4111111111111111",
        "iban": "GB82WEST12345698765432",
        "spanish_dni_nie": "12345678Z",
        "phone": "+34600123456",
        "us_ssn": "123-45-6789",
        "us_itin": "900-70-1234",
        "passport": "X1234567",
    }
    for typ, raw in samples.items():
        preview = detectors.mask(typ, raw)
        assert raw not in preview, f"{typ}: preview leaks full value"
        assert preview != raw, f"{typ}: preview equals raw"
        # Reveal at most the last 4 characters verbatim.
        assert raw[:-4] not in preview or len(raw) <= 4, f"{typ}: reveals too much"


def test_mask_credit_card_last4_only():
    assert detectors.mask("credit_card", "4111111111111111") == "************1111"


def test_mask_ssn_last4_only():
    assert detectors.mask("us_ssn", "123-45-6789") == "***-**-6789"


# ── phone precision (regression) ─────────────────────────────────────────────

def test_phone_rejects_non_phone_numeric_patterns():
    # dates, versions, coordinates, delimited number lists, low-entropy runs.
    for s in [
        "SSN placeholder 000-00-0000 redacted",   # low-entropy (all-repeat)
        "date range 2020-2024-1999 x",            # 12 digits, dashes
        "version 1.2.3.4567.8901 build",          # dot-separated
        "coords 40.7128 -74.0060 loc",            # dot-separated
        "range 100 200 300 400 sum",              # 12 digits, spaces
    ]:
        assert detectors.find_phone(s) == [], s
        assert [f.type for f in detectors.run_detectors(s)] == [], s


def test_phone_still_matches_real_numbers():
    assert detectors.find_phone("call +34600123456 now")[0].type == "phone"
    assert detectors.find_phone("US 415-555-0132 line")[0].raw == "415-555-0132"
    assert detectors.find_phone("call +44 20 7946 0958")[0].raw.strip() == "+44 20 7946 0958"


def test_phone_mask_keeps_last4_digits_only():
    assert detectors.mask("phone", "+34600123456") == "+*******3456"
    assert detectors.mask("phone", "415-555-0132") == "***-***-0132"


# ── dedupe: containment prefers the more complete PII string ──────────────────

def test_email_with_embedded_card_is_reported_as_email():
    # A Luhn-valid digit run inside an email local part must not hide the email.
    findings = _run("contact user4111111111111111@x.com now")
    assert _types(findings) == ["email"]
    assert "4111111111111111" not in findings[0].preview


# ── types selection semantics ─────────────────────────────────────────────────

def test_empty_types_selects_none():
    # An explicit empty list selects no detectors (None means "all").
    assert _run("mail alice@example.com card 4111 1111 1111 1111", types=[]) == []


# ── passport: adjacent keywords don't double-report ──────────────────────────

def test_passport_adjacent_keywords_report_once():
    assert len(detectors.find_passport("passport passport X1234567")) == 1


# ── registry: no detector can inherit a silent disclosure decision ────────────

def test_every_detector_declares_its_own_priority_and_masker():
    """A detector cannot be registered without deciding how much it reveals.

    ``Detector`` requires all four fields, and ``DETECTOR_NAMES`` is derived from
    the same records — so the parallel-dict drift that a default masker or a
    default priority used to hide is now impossible to express.
    """
    assert tuple(d.name for d in detectors._REGISTRY) == detectors.DETECTOR_NAMES
    assert isinstance(detectors.DETECTOR_NAMES, tuple)  # source of truth, not mutable
    for d in detectors._REGISTRY:
        assert callable(d.find) and callable(d.mask)
        assert isinstance(d.priority, int)


def test_mask_raises_on_unregistered_type():
    # Unreachable from a scan, so a raise beats guessing a masker for a type
    # whose disclosure profile nobody decided.
    with pytest.raises(KeyError):
        detectors.mask("api_token", "sk-ABCDEFGHIJKLMNOPQRSTUV")


def test_registry_rejects_duplicate_priority():
    clash = detectors.Detector("api_token", lambda t: [], priority=0, mask=str)
    with pytest.raises(ValueError, match="priority"):
        detectors._validate_registry(detectors._REGISTRY + (clash,))


def test_registry_rejects_duplicate_name():
    clash = detectors.Detector("email", lambda t: [], priority=99, mask=str)
    with pytest.raises(ValueError, match="name"):
        detectors._validate_registry(detectors._REGISTRY + (clash,))


# ── FALSE NEGATIVES: a rejected greedy match must not hide valid PII ──────────
#
# The gate's one unforgivable failure is `clean: true` with PII in the file.
# A greedy regex routinely matches a superset of a real value; if the validator
# then rejects it and the search resumes past the whole span, the value inside
# is never seen. Each case below reported `clean: true` before the fix.

def test_card_hidden_behind_a_rejected_greedy_match():
    # _CARD_RE matches "12345 4111 1111 1111" (17 digits, Luhn-invalid).
    text = "order 12345 4111 1111 1111 1111 charged"
    assert [m.raw for m in detectors.find_credit_card(text)] == ["4111 1111 1111 1111"]
    assert _types(_run(text)) == ["credit_card"]


def test_iban_hidden_behind_a_rejected_greedy_match():
    # _IBAN_RE matches "XX00 GB82 WEST ..." as one mod-97-invalid run.
    text = "Ref XX00 GB82 WEST 1234 5698 7654 32"
    assert [m.raw for m in detectors.find_iban(text)] == ["GB82 WEST 1234 5698 7654 32"]
    assert _types(_run(text)) == ["iban"]


def test_iban_hidden_across_a_newline():
    text = "AB12\nGB82 WEST 1234 5698 7654 32"
    assert [m.raw for m in detectors.find_iban(text)] == ["GB82 WEST 1234 5698 7654 32"]


def test_phone_deliberately_does_not_rescan_a_rejected_run():
    """The phone rule judges the *maximal* digit run, so it must not retry.

    Retrying would recover the number in the first case, at the cost of reading a
    9-11 digit sub-window out of every numeric table (the other two). The phone
    detector has no checksum to fall back on, so it keeps the precision instead.
    """
    assert detectors.find_phone("22 33 44 55 415-555-0132") == []      # known miss
    assert detectors.find_phone("range 100 200 300 400 sum") == []     # what it buys
    assert detectors.find_phone("ids 11 22 33 44 55 66 77 88") == []


def test_rejected_match_does_not_admit_a_mid_token_match():
    # Resuming at start+1 must not let a match begin inside a digit run: the
    # lookbehind still sees the character before the resume point.
    assert detectors.find_credit_card("num 4111 1111 1111 1112 nope") == []
    assert detectors.find_iban("ES0000000000000000000000 is bogus") == []
    assert detectors.find_spanish_dni_nie("id 12345678A invalid") == []


# ── phone precision: log timestamps are not phone numbers ────────────────────

def test_phone_rejects_log_timestamps():
    for s in [
        "2024-01-15 10:30:45",              # the default `logging` / syslog stamp
        "[2023-11-02 08:15:00] INFO up",
        "build 2021-07-04 12:00:01 done",
        "at 2024-01-15 10",                 # truncated to hours, no ':' to key on
        "15-01-2024 09:22:01",              # day-first
    ]:
        assert detectors.find_phone(s) == [], s
        assert detectors.run_detectors(s) == [], s


def test_phone_separators_do_not_span_lines_or_columns():
    for s in ["order 12345\n6789 shipped", "ref 12345\xa06789 x", "a 1234\t56789 b"]:
        assert detectors.find_phone(s) == [], s


def test_phone_still_matches_after_a_colon():
    # ':' is excluded by the *lookahead* only — a number may still follow one.
    assert detectors.find_phone("phone:415-555-0132 x")[0].raw == "415-555-0132"


# ── passport: window handling ────────────────────────────────────────────────

def test_passport_reports_every_token_in_the_window():
    assert [m.raw for m in detectors.find_passport("passport A123456 B234567")] == [
        "A123456", "B234567",
    ]


def test_passport_does_not_report_a_token_truncated_by_the_window():
    # The real token runs past the 30-char window; reporting its prefix would be
    # a partial value with a bogus span.
    text = "passport" + " " * 21 + "AB123456789"
    assert detectors.find_passport(text) == []


# ── DNI/NIE case handling is symmetric ───────────────────────────────────────

def test_dni_nie_accepts_either_case():
    for s in ["nie x1234567l", "nie X1234567l", "nie X1234567L"]:
        assert [m.raw.upper() for m in detectors.find_spanish_dni_nie(s)] == ["X1234567L"], s
