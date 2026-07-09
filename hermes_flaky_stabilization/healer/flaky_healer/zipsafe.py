"""Bounded zip reads — defense against decompression bombs (zip bombs).

``zipfile`` performs no size accounting: ``ZipFile.read(name)`` decompresses a
whole member into memory, so a few-KB archive can expand to gigabytes and
exhaust the host. Traces are fully attacker-controlled and CI-log archives are
only semi-trusted, so every read on this path goes through a ``ZipBudget``:

* per-member cap (``MAX_ENTRY_BYTES``) — enforced by a *bounded* streaming read
  (``ZipExtFile.read(n)`` decompresses at most ``n`` bytes), so a member that
  lies about its declared ``file_size`` in the central directory cannot beat it;
* a cumulative cap (``MAX_TOTAL_BYTES``) across every read sharing one budget;
* an entry-count cap (``guard_entry_count``) so an archive of millions of tiny
  members cannot blow up ``namelist()`` / iteration.
"""

from __future__ import annotations

import codecs
import zipfile

# Generous for real Playwright traces (tens of MB) and filtered CI-log archives,
# while still bounding a malicious archive to a recoverable amount of memory.
MAX_ENTRY_BYTES = 64 * 1024 * 1024  # 64 MiB per member
MAX_TOTAL_BYTES = 256 * 1024 * 1024  # 256 MiB across one budget
MAX_ENTRIES = 10_000


class ZipLimitError(Exception):
    """A zip member, the archive total, or the entry count exceeded a safety cap."""


def guard_entry_count(zf: zipfile.ZipFile, limit: int = MAX_ENTRIES) -> None:
    """Reject archives with an absurd number of members before iterating them."""
    count = len(zf.infolist())
    if count > limit:
        raise ZipLimitError(f"zip archive has {count} entries, exceeding the {limit} cap")


class ZipBudget:
    """A shared decompression budget for a single archive.

    ``read`` returns the member's bytes or raises ``ZipLimitError``; it never
    decompresses more than the smaller of the per-entry cap and what remains of
    the cumulative budget, regardless of the member's declared size.
    """

    def __init__(
        self, max_total: int = MAX_TOTAL_BYTES, max_entry: int = MAX_ENTRY_BYTES
    ) -> None:
        self.remaining = max_total
        self.max_entry = max_entry

    def read(self, zf: zipfile.ZipFile, name: str) -> bytes:
        cap = min(self.max_entry, self.remaining)
        with zf.open(name) as fh:
            # Read one byte past the cap so we can detect (not just truncate) an
            # over-cap member; ZipExtFile decompresses lazily up to this bound.
            data = fh.read(cap + 1)
        if len(data) > cap:
            raise ZipLimitError(
                f"zip entry {name!r} exceeds the {self.max_entry}-byte safety cap "
                "(possible decompression bomb)"
            )
        self.remaining -= len(data)
        return data

    def iter_lines(self, zf: zipfile.ZipFile, name: str, chunk_size: int = 1 << 20):
        """Yield the member's text lines without materializing it whole.

        Enforces the same per-entry and cumulative caps as ``read`` (a member is
        decompressed in bounded chunks; one byte past the cap trips the
        bomb guard), but keeps only one chunk plus the current partial line in
        memory instead of the full decompressed bytes + decoded string + line
        list. Lines are split on ``\\n``; a trailing ``\\r`` (CRLF input) rides
        along for the caller to ``strip()``. The cumulative budget is charged
        once the generator is fully consumed.
        """
        cap = min(self.max_entry, self.remaining)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        consumed = 0
        pending = ""
        with zf.open(name) as fh:
            while True:
                chunk = fh.read(min(chunk_size, cap - consumed + 1))
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > cap:
                    raise ZipLimitError(
                        f"zip entry {name!r} exceeds the {self.max_entry}-byte safety cap "
                        "(possible decompression bomb)"
                    )
                pending += decoder.decode(chunk)
                nl = pending.rfind("\n")
                if nl >= 0:
                    head, pending = pending[:nl], pending[nl + 1:]
                    yield from head.split("\n")
            pending += decoder.decode(b"", final=True)
        if pending:
            yield pending
        self.remaining -= consumed
