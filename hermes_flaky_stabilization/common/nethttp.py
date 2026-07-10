"""Shared outbound-HTTP construction rules (stdlib only).

The plugin has two credential-bearing HTTP clients — the Jira client (Basic /
Bearer auth) and the healer's GitHub CI transport (Bearer) — plus the triage
log fetcher. They each build a ``urllib`` request, and the one security rule
they MUST share is easy to get wrong independently: a credential header must be
attached *unredirected* so ``urllib``'s redirect handler never replays it to a
redirect target. GitHub serves run logs as a 302 to a pre-signed blob URL on a
different host; a proxy or an http→https bounce can redirect too — replaying the
``Authorization`` there leaks the token.

This module owns that rule once. The healer transport already did the right
thing; the Jira transport did not (it added ``Authorization`` as an ordinary
header), so both now route through :func:`build_request`.

This is deliberately NOT the triage SSRF-guarded opener (``triage/safehttp.py``):
that opener carries a triage-specific private-IP policy which, per decision S2
(``docs/DECISIONS.md``), must not be imposed on the GitHub-Enterprise transport.
Only the auth-on-redirect invariant is shared here.

Pure standard library; imports nothing from the plugin.
"""

from __future__ import annotations

import urllib.request

# Header names whose value is a credential. Attached via
# ``add_unredirected_header`` so ``urllib``'s redirect handler — which replays
# ordinary headers but not unredirected ones — can never forward them to a
# redirect target on another host.
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


def build_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> urllib.request.Request:
    """A ``urllib`` request with credential headers attached unredirected.

    Non-sensitive headers are added normally. ``method`` is upper-cased. The
    caller owns transport concerns (timeout, TLS context, response reading);
    this only encodes the "don't leak auth on a redirect" construction rule so
    every outbound client shares it by default.
    """
    req = urllib.request.Request(url, data=body, method=method.upper())
    for key, value in dict(headers or {}).items():
        if key.lower() in SENSITIVE_HEADERS:
            req.add_unredirected_header(key, value)
        else:
            req.add_header(key, value)
    return req
