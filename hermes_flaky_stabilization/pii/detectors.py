"""PII detectors + validators for hermes-masking-validator.

Each detector is a pure function ``find_<name>(text) -> list[Match]``. Validators
(Luhn, IBAN mod-97, Spanish DNI/NIE mod-23 control letter, US SSN/ITIN structural
rules) cut false positives so a "clean" scan is trustworthy.

Raw PII values live only inside this module: ``run_detectors`` returns
``Finding`` objects that carry a **masked** preview and never the raw value, so
callers (the scanner, the tool result, logs) cannot leak personal data.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from operator import attrgetter


@dataclass(frozen=True)
class Match:
    """An internal detector hit, including the raw matched span."""

    type: str
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class Finding:
    """A safe, outward-facing hit — masked preview only, never the raw value."""

    type: str
    start: int
    end: int
    preview: str


# ── validators ───────────────────────────────────────────────────────────────

# Compiled once at import. These run once per candidate match rather than once
# per file, so going through the `re` module-level functions (which re-look-up
# the pattern cache on every call) is the single hottest overhead in a scan.
_NON_DIGIT_RE = re.compile(r"\D")
_CARD_SEP_RE = re.compile(r"[ -]")
_IBAN_COMPACT_RE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
_DNI_FORM_RE = re.compile(r"\d{8}[A-Z]")
_NIE_FORM_RE = re.compile(r"[XYZ]\d{7}[A-Z]")


def luhn_ok(digits: str) -> bool:
    """ISO/IEC 7812 Luhn checksum over a bare digit string."""
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(iban: str) -> bool:
    """ISO 7064 mod-97-10 validation of an IBAN (spaces allowed)."""
    s = iban.replace(" ", "").upper()
    if not _IBAN_COMPACT_RE.fullmatch(s):
        return False
    rearranged = s[4:] + s[:4]
    # A→10 … Z→35, digits unchanged; int(ch, 36) does exactly this mapping.
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_NIE_PREFIX = {"X": "0", "Y": "1", "Z": "2"}


def dni_nie_ok(value: str) -> bool:
    """Validate a Spanish DNI/NIE by its modulo-23 control letter."""
    s = value.upper()
    if _DNI_FORM_RE.fullmatch(s):
        number, letter = int(s[:8]), s[8]
    elif _NIE_FORM_RE.fullmatch(s):
        number, letter = int(_NIE_PREFIX[s[0]] + s[1:8]), s[8]
    else:
        return False
    return _DNI_LETTERS[number % 23] == letter


def _ssn_ok(area: str, group: str, serial: str) -> bool:
    """SSA structural rules: areas 000, 666 and 900–999 were never issued as
    SSNs (900+ is ITIN space), and no block may be all zeros."""
    return (
        int(area) not in (0, 666)
        and int(area) < 900
        and int(group) != 0
        and int(serial) != 0
    )


def _itin_ok(area: str, group: str, serial: str) -> bool:
    """IRS ITIN rules: area starts with 9, and the group (4th–5th digits) falls
    in one of the assigned ranges 50–65, 70–88, 90–92, 94–99."""
    group_num = int(group)
    return (
        area.startswith("9")
        and (
            50 <= group_num <= 65
            or 70 <= group_num <= 88
            or 90 <= group_num <= 92
            or 94 <= group_num <= 99
        )
        and int(serial) != 0
    )


def _card_ok(raw: str) -> bool:
    """A card number is 13–19 digits (separators allowed) with a valid Luhn check."""
    digits = _CARD_SEP_RE.sub("", raw)
    return 13 <= len(digits) <= 19 and luhn_ok(digits)


# ── detectors ────────────────────────────────────────────────────────────────

# Quantifiers are length-bounded (RFC 5321: local part <=64, domain <=255) so a
# long run of local-part characters with no '@' cannot trigger quadratic
# backtracking — an unbounded `[...]+@` is O(n^2) on such input and hangs the
# scan on ordinary blobs (base64url, JWTs, tokens all sit in the local class).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
# Both the NIE prefix and the control letter accept either case; ``dni_nie_ok``
# upper-cases before checking, so accepting only one of the two was arbitrary.
_DNI_RE = re.compile(r"\b(?:[XYZxyz]\d{7}|\d{8})[A-Za-z]\b")
_SSN_LIKE_RE = re.compile(r"(?<!\d)(\d{3})[- ](\d{2})[- ](\d{4})(?!\d)")
# '.' is deliberately not a phone separator: it collides with version strings,
# IP addresses, coordinates, and decimals far more than it helps recall. Nor is
# `\s`: it spans newlines, tabs and NBSP, which would splice a number out of two
# adjacent table columns or consecutive log lines. A real phone number is written
# with spaces, and nothing else.
#
# The trailing `(?!\w|:)` rejects `YYYY-MM-DD HH` sliced out of a `HH:MM:SS`
# timestamp — the single most common phone-shaped token in a QA log.
_PHONE_RE = re.compile(r"(?<![\w+])(\+?)\d[\d ()\-]{6,17}\d(?![\w:])")
# ...and this rejects the same date when the timestamp has no time-of-day after
# it (`... 2024-01-15 10` at end of line), which no lookahead can see.
_DATE_LIKE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}")
_PASSPORT_CTX_RE = re.compile(r"passport|pasaporte", re.IGNORECASE)
_PASSPORT_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9]*[0-9])[A-Z0-9]{6,9}\b")
_PASSPORT_WINDOW = 30
_WORD_RE = re.compile(r"\w")


def _validated_finder(
    name: str,
    pattern: re.Pattern[str],
    is_valid: Callable[[re.Match[str]], bool],
) -> Callable[[str], list[Match]]:
    """Build a detector from a regex plus a validator run on each regex match.

    Precision over recall: every detector that *can* check its hits against a
    checksum or structural rule does so here, so a ``clean`` scan is trustworthy.

    On rejection the search resumes at ``m.start() + 1``, **not** past the
    rejected span. A greedy pattern routinely swallows a valid value inside an
    invalid one — ``12345 4111 1111 1111 1111`` matches ``_CARD_RE`` as 17
    Luhn-invalid digits that *contain* a valid card, and ``XX00 GB82 WEST …``
    matches ``_IBAN_RE`` as one mod-97-invalid run that contains a valid IBAN.
    Skipping to ``m.end()`` after a failed check hid both: the gate reported
    ``clean`` with the PII sitting in the file. ``search(text, pos)`` (rather
    than a slice) keeps ``\\b`` and lookbehind aware of the text before *pos*,
    so no new match can start mid-token.

    The cost is paid in false positives, and only where the validator is weak.
    Luhn rejects just 9 in 10 candidates, so examining every card-shaped window
    of a long digit table instead of only the leftmost one raises the hit rate on
    random 4-digit groups from ~14% to ~35%. That is the right trade for a gate:
    a spurious ``clean: false`` costs a human a glance, a missed card costs a
    cardholder. Mod-97 (IBAN) and mod-23 (DNI/NIE) barely move.

    **Only for validators that are checksums or fixed-width structural rules.**
    ``find_phone`` deliberately does not use this: its rule *is* "the maximal
    digit run must be phone-shaped", so retrying inside a rejected run would
    re-admit every sub-window and read ``100 200 300 400`` as a phone number.
    """
    def find(text: str) -> list[Match]:
        out: list[Match] = []
        pos = 0
        while (m := pattern.search(text, pos)) is not None:
            if is_valid(m):
                out.append(Match(name, m.start(), m.end(), m.group(0)))
                # max(): a zero-width match would otherwise never advance.
                pos = max(m.end(), m.start() + 1)
            else:
                pos = m.start() + 1
        return out

    find.__name__ = f"find_{name}"
    find.__qualname__ = find.__name__
    return find


# An email is validated by its shape alone; the regex is the whole rule.
find_email = _validated_finder("email", _EMAIL_RE, lambda m: True)
find_credit_card = _validated_finder("credit_card", _CARD_RE, lambda m: _card_ok(m.group(0)))
find_iban = _validated_finder("iban", _IBAN_RE, lambda m: iban_ok(m.group(0)))
find_spanish_dni_nie = _validated_finder("spanish_dni_nie", _DNI_RE, lambda m: dni_nie_ok(m.group(0)))

# us_ssn and us_itin share _SSN_LIKE_RE and are disjoint by construction:
# _ssn_ok rejects areas >= 900, which is exactly what _itin_ok requires. That
# disjointness is why a 9xx number is never reported as both — preserve it if
# you touch either validator.
find_us_ssn = _validated_finder("us_ssn", _SSN_LIKE_RE, lambda m: _ssn_ok(*m.groups()))
find_us_itin = _validated_finder("us_itin", _SSN_LIKE_RE, lambda m: _itin_ok(*m.groups()))


# phone and passport carry no checksum, so neither fits the pattern+validator
# mould above; both need bespoke gating and correspondingly closer scrutiny.

def find_phone(text: str) -> list[Match]:
    # Ordinary logs are dense in phone-shaped-but-not-phone tokens (a
    # `YYYY-MM-DD HH:MM:SS` stamp carries a 10-digit run with separators), so the
    # rejection path runs far more often than the accept path and the cheapest
    # test is applied first.
    #
    # `finditer`, NOT the resume-at-start+1 loop that `_validated_finder` uses.
    # The tests below judge the *maximal* digit run, so a rejection means "this
    # run is not a phone number" — not "no phone number starts here". Rescanning
    # a rejected run would find a 9-11 digit sub-window in any numeric table:
    # `100 200 300 400` would report `200 300 400`, and `11 22 33 44 55 66 77 88`
    # would report `44 55 66 77 88`. The known cost is a phone number butted
    # directly against a digit table (`22 33 44 55 415-555-0132`) being missed.
    out = []
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0)
        # A date is 8-10 digits with dashes and would otherwise pass every test
        # below. Anchored at the start of the span: a real number never opens
        # with a date.
        if _DATE_LIKE_RE.match(raw):
            continue
        digits = _NON_DIGIT_RE.sub("", raw)
        count = len(digits)
        if m.group(1):  # leading '+' → international, separators optional
            ok = 8 <= count <= 15
        else:
            # National numbers: require an explicit separator (else a bare digit
            # run is too ambiguous to call a phone number). Capped at 11 digits
            # so 12+-digit date ranges / delimited number lists don't match.
            # Without a '+', every character of *raw* is either a digit or one of
            # _PHONE_RE's separators, so a length mismatch *is* a separator.
            ok = 9 <= count <= 11 and count != len(raw)
        # Low-entropy runs (e.g. 000-00-0000, 1111 1111 111) are never real
        # phone numbers — they are placeholders or padding.
        if ok and len(set(digits)) > 1:
            out.append(Match("phone", m.start(), m.end(), raw))
    return out


def find_passport(text: str) -> list[Match]:
    """Context-gated: report passport-shaped tokens that follow a
    ``passport``-family keyword within a short window. Passport numbers carry
    no universal checksum, so requiring context keeps false positives low.

    The window is applied via ``search``'s *endpos* rather than by slicing, so
    ``\\b`` still sees the character *before* the window and a token cannot be
    matched from its middle. ``endpos`` does make the window look like the end of
    the string, so a token running past it would satisfy the closing ``\\b``
    spuriously — hence the explicit check against the real next character.
    """
    out = []
    seen: set[tuple[int, int]] = set()
    for ctx in _PASSPORT_CTX_RE.finditer(text):
        window_end = min(ctx.end() + _PASSPORT_WINDOW, len(text))
        pos = ctx.end()
        while (tok := _PASSPORT_TOKEN_RE.search(text, pos, window_end)) is not None:
            start, end = tok.start(), tok.end()
            pos = end
            if end < len(text) and _WORD_RE.match(text, end):
                continue  # truncated by the window; the real token is longer
            # Adjacent keywords (e.g. "passport passport X123456") can point at
            # the same token; report each span once.
            if (start, end) in seen:
                continue
            seen.add((start, end))
            out.append(Match("passport", start, end, text[start:end]))
    return out


# ── masking (safe previews) ───────────────────────────────────────────────────

# How much of a real value a preview reveals is a disclosure decision, so it must
# never fall back to a default: every entry in `_REGISTRY` below names its masker
# explicitly, and the parameterised maskers here take an explicit *keep* count.

def _mask_keeping_separators(raw: str, keep: int) -> str:
    """Star out every digit except the last *keep*, leaving separators in place.

    ``123-45-6789`` → ``***-**-6789``.
    """
    total = sum(c.isdigit() for c in raw)
    seen = 0
    out = []
    for c in raw:
        if c.isdigit():
            seen += 1
            out.append(c if seen > total - keep else "*")
        else:
            out.append(c)
    return "".join(out)


def _mask_stripping_separators(raw: str, keep: int) -> str:
    """Drop every separator, then star out all but the last *keep* digits.

    ``4111 1111 1111 1111`` → ``************1111``.
    """
    digits = _NON_DIGIT_RE.sub("", raw)
    if len(digits) <= keep:
        return "*" * len(digits)
    return "*" * (len(digits) - keep) + digits[-keep:]


def _mask_email(raw: str) -> str:
    local, _, domain = raw.partition("@")
    masked_local = (local[:1] or "") + "***"
    if "." in domain:
        tld = domain.rpartition(".")[2]
        masked_domain = (domain[:1] or "") + "****." + tld
    else:
        masked_domain = (domain[:1] or "") + "****"
    return f"{masked_local}@{masked_domain}"


def _mask_iban(raw: str) -> str:
    """Reveal the country code and the last 4; star the rest."""
    s = raw.replace(" ", "")
    if len(s) <= 6:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 6) + s[-4:]


def _mask_tail(raw: str, keep: int) -> str:
    """Star out all but the last *keep* characters, separators included."""
    if len(raw) <= keep:
        return "*" * len(raw)
    return "*" * (len(raw) - keep) + raw[-keep:]


# ── detector registry ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Detector:
    """A detector and every decision that must travel with it.

    Binding the finder, its overlap priority and its masker into one record makes
    it structurally impossible to register a detector without deciding how much of
    a match it reveals: the constructor is the check. This replaced three parallel
    dicts keyed by detector name, which could silently drift apart — and whose
    fallbacks (a default masker, a default priority) papered over the omission
    instead of surfacing it.
    """

    name: str
    find: Callable[[str], list[Match]]
    priority: int  # lower wins when two detectors claim overlapping spans
    mask: Callable[[str], str]


# Tuple order is the default run order. `priority` is a separate axis: it ranks
# detectors by confidence when their spans overlap, most specific first.
_REGISTRY: tuple[Detector, ...] = (
    Detector("email", find_email, priority=6, mask=_mask_email),
    Detector("credit_card", find_credit_card, priority=0,
             mask=lambda r: _mask_stripping_separators(r, keep=4)),
    Detector("iban", find_iban, priority=1, mask=_mask_iban),
    Detector("spanish_dni_nie", find_spanish_dni_nie, priority=4,
             mask=lambda r: _mask_tail(r, keep=1)),  # reveal only the control letter
    Detector("phone", find_phone, priority=7,
             mask=lambda r: _mask_keeping_separators(r, keep=4)),
    Detector("us_ssn", find_us_ssn, priority=2,
             mask=lambda r: _mask_keeping_separators(r, keep=4)),
    Detector("us_itin", find_us_itin, priority=3,
             mask=lambda r: _mask_keeping_separators(r, keep=4)),
    # No checksum and no fixed width, so reveal the least of any type.
    Detector("passport", find_passport, priority=5,
             mask=lambda r: _mask_tail(r, keep=2)),
)


def _validate_registry(registry: tuple[Detector, ...]) -> None:
    """Reject a registry that cannot resolve an overlap deterministically.

    A duplicate priority would leave the winner to be decided by span length and
    position alone — a silent, input-dependent choice in a module whose whole
    premise is that a ``clean`` verdict is trustworthy. Checked with a raise, not
    an ``assert``, so ``python -O`` cannot strip it.
    """
    names = [d.name for d in registry]
    if len(set(names)) != len(names):
        raise ValueError("duplicate detector name in _REGISTRY")
    priorities = [d.priority for d in registry]
    if len(set(priorities)) != len(priorities):
        raise ValueError("duplicate detector priority in _REGISTRY")


_validate_registry(_REGISTRY)

DETECTORS: dict[str, Detector] = {d.name: d for d in _REGISTRY}
# A tuple, not a list: this is the single source of truth that `scanner.scan`
# validates against and that `register()` injects into the tool schema, so it
# must not be mutable by a caller who happens to hold a reference.
DETECTOR_NAMES: tuple[str, ...] = tuple(d.name for d in _REGISTRY)


def mask(match_type: str, raw: str) -> str:
    """Render a safe, masked preview for a raw value of *match_type*.

    Raises ``KeyError`` for an unregistered type. That is unreachable from a scan
    — every ``Match.type`` is produced by a registered detector — so a raise here
    means a programmer error. Guessing a masker for an unknown type is the one
    thing this module must never do.
    """
    return DETECTORS[match_type].mask(raw)


# ── orchestration ─────────────────────────────────────────────────────────────

def _drop_contained(matches: list[Match]) -> list[Match]:
    """Drop every match whose span sits strictly inside another match's span.

    The larger span is the more complete PII string, so an email whose local part
    happens to contain a Luhn-valid digit run is reported as the email, not as a
    card. Sorting by (start asc, end desc) means any span that can contain the
    current one has already been seen, so a single sweep suffices.
    """
    spans = sorted({(m.start, m.end) for m in matches}, key=lambda s: (s[0], -s[1]))
    contained: set[tuple[int, int]] = set()
    widest_end = -1
    for span in spans:
        _, end = span
        if end <= widest_end:  # an earlier, strictly wider span covers this one
            contained.add(span)
        else:
            widest_end = end
    return [m for m in matches if (m.start, m.end) not in contained]


_span_key = attrgetter("start", "end")  # C-level; a lambda here is a hot-loop cost


def _claim_non_overlapping(matches: list[Match]) -> list[Match]:
    """Let matches claim their span in confidence order; skip any that collide.

    Confidence is (detector priority, then longer span, then earlier start), so
    an equal-length overlap such as SSN vs. phone resolves to the higher-priority
    detector.

    ``_drop_contained`` has already removed every span that sits inside another,
    so once the matches are sorted by span, ``starts`` and ``ends`` are *both*
    non-decreasing. The spans overlapping any given span therefore sit
    contiguously around it, and a collision check only has to walk outward while
    the neighbours still reach across — a couple of comparisons unless PII is
    genuinely stacked. Holding the claimed set in a sorted list instead costs an
    O(k) ``list.insert`` per match: 335 ms on a PII-dense 2 MB file, against
    ~20 ms here.
    """
    if not matches:
        return []

    ordered = sorted(matches, key=_span_key)
    starts = [m.start for m in ordered]
    ends = [m.end for m in ordered]
    total = len(ordered)

    # Nothing overlaps its neighbour, so nothing overlaps at all and confidence
    # never has to break a tie. Worth its own pass: a scan of ordinary evidence
    # finds PII scattered, not stacked, and this is then the whole function.
    if all(starts[p] >= ends[p - 1] for p in range(1, total)):
        return ordered

    claimed = bytearray(total)
    # Materialised once rather than looked up inside the sort key: this runs only
    # on the overlap path, but a dict lookup plus attribute access per comparison
    # is the kind of cost the fast path above exists to avoid.
    priorities = [DETECTORS[m.type].priority for m in ordered]
    by_confidence = sorted(
        range(total),
        key=lambda p: (priorities[p], starts[p] - ends[p], starts[p]),
    )
    for p in by_confidence:
        start, end = starts[p], ends[p]
        collides = False
        # Leftward: ends are non-decreasing, so the first neighbour that stops
        # short of *start* proves every one before it does too. A duplicate span
        # sorts adjacent and reaches across, so it collides — as it should.
        q = p - 1
        while q >= 0 and ends[q] > start:
            if claimed[q]:
                collides = True
                break
            q -= 1
        # Rightward: starts are non-decreasing, so the mirror argument holds.
        q = p + 1
        while not collides and q < total and starts[q] < end:
            if claimed[q]:
                collides = True
            q += 1
        if not collides:
            claimed[p] = 1
    return [m for p, m in enumerate(ordered) if claimed[p]]  # ordered by start


def _dedupe(matches: list[Match]) -> list[Match]:
    """Resolve overlapping matches into a non-overlapping set, ordered by start."""
    return _claim_non_overlapping(_drop_contained(matches))


def run_detectors(text: str, types: list[str] | None = None) -> list[Finding]:
    """Run the selected detectors over *text* and return masked ``Finding``s.

    *types* restricts the run to a subset of detector names; unknown names are
    ignored here (the tool handler validates them and reports a structured
    error). ``None`` runs every detector; an explicit empty list selects none.
    Overlapping matches are de-duplicated by detector priority.
    """
    if types is None:
        selected: tuple[str, ...] | list[str] = DETECTOR_NAMES
    else:
        wanted = set(types)  # `types` is caller-supplied and may be long
        selected = [name for name in DETECTOR_NAMES if name in wanted]
    matches: list[Match] = []
    for name in selected:
        matches.extend(DETECTORS[name].find(text))
    return [
        Finding(m.type, m.start, m.end, mask(m.type, m.raw))
        for m in _dedupe(matches)
    ]
