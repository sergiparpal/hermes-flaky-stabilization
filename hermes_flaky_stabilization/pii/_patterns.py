"""Shared regex *bodies* for the PII detectors and the outbound redactor.

This module holds only the fragments that are provably identical between
:mod:`.detectors` (the precision-biased scanner) and :mod:`.redaction` (the
recall-biased outbound path). Each module composes its own full pattern from a
body here, keeping its own anchors and bounds — the extraction exists to keep
the *shared* part from drifting between the two, not to unify the patterns.

IMPORTANT — do NOT merge the SSN or credit-card patterns across the two modules.
They are INTENTIONALLY different and must stay that way:

* SSN: detectors use ``(\\d{3})[- ](\\d{2})[- ](\\d{4})`` with a structural
  validator (``_ssn_ok``/``_itin_ok``) — a precision gate so a ``clean`` verdict
  is trustworthy. Redaction uses ``\\d{3}[ \\t\\-]\\d{2}[ \\t\\-]\\d{4}`` with no
  validator — recall-biased, because on the outbound path a missed SSN leaks.
* Credit card: detectors gate on Luhn (``_card_ok``); redaction matches any
  13–16 digit group with no checksum. Same precision-vs-recall split.

Only the email *body* below is genuinely common to both.
"""

# Shared local+domain body of the email pattern, up to and including the dot
# before the TLD. Each module appends its own TLD bound (detectors: ``{2,24}``;
# redaction: ``{2,}``) and its own anchors (redaction wraps in ``\b``), so the
# fully composed patterns are NOT identical — only this prefix is.
#
# Quantifiers are length-bounded (RFC 5321: local part <=64, domain <=255) so a
# long run of local-part characters with no '@' cannot trigger quadratic
# backtracking — an unbounded `[...]+@` is O(n^2) on such input and hangs the
# scan on ordinary blobs (base64url, JWTs, tokens all sit in the local class).
# The local class is `\w` (Unicode word chars — `str` patterns are Unicode by
# default) plus the RFC specials, so an internationalized (EAI/IDN) local part
# such as `josé@…` or `иван@…` is not silently treated as clean by the gate and
# is redacted on the outbound path.
EMAIL_BODY = r"[\w.%+\-]{1,64}@[\w.\-]{1,255}\."
