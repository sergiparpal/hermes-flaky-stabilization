"""Filesystem scanner + result assembly for hermes-masking-validator.

Walks a file or directory, classifies each file (text / image / other binary),
routes images to the runtime-gated OCR path, runs the detectors, and assembles
the structured, **masked** result contract. Read-only: it opens files only for
reading and never writes anything.
"""
import os
import time
from collections import Counter
from collections.abc import Callable
from functools import cache
from typing import Any

# Unified-plugin adaptation: the legacy dual-import shim is gone — the package
# gives this module a stable import root, so the relative import is the only
# path (plan Phase 3 task 1).
from . import detectors

# ── bounds (protect memory + the prompt cache) ───────────────────────────────
MAX_BYTES_PER_FILE = 2_000_000     # per-file read cap
DEFAULT_MAX_FILES = 2000           # default file cap when caller omits max_files
MAX_MAX_FILES = 10_000             # ceiling the caller may *not* raise
MAX_FINDINGS = 1000                # cap on findings returned
MAX_SCAN_SECONDS = 300             # wall-clock ceiling on the per-file loop
_NULL_SCAN_BYTES = 8192            # bytes inspected for the null-byte heuristic

# Every param is untrusted, so the *error* path needs bounds too: without them a
# caller can inflate the result dict (and the log line) with megabytes of its own
# input. Anything longer than these is rejected without being echoed at all.
MAX_TARGET_LEN = 4096              # PATH_MAX; nothing longer can name a real file
MAX_TYPE_NAME_LEN = 64
MAX_TYPES = 32
_MAX_ERROR_FIELD = 120             # chars of a (masked) value an error may quote

# OCR resource bounds (images are untrusted): cap the decoded pixel count against
# decompression bombs and wall-clock the tesseract call so a crafted image can't
# exhaust memory or wedge the scan. Both breaches surface as reason=ocr_failed.
OCR_TIMEOUT_SECONDS = 30
MAX_IMAGE_PIXELS = 40_000_000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}

_TYPES_REMEDIATION = (
    "Omit 'types' to run all detectors, or pass e.g. [\"email\", \"credit_card\"]."
)


def _error(message: str, remediation: str) -> dict[str, Any]:
    return {"success": False, "error": message, "remediation": remediation}


# ── OCR (runtime-gated) ──────────────────────────────────────────────────────

def _ocr_available() -> bool:
    """True only if both ``pytesseract`` and a ``tesseract`` binary are present."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_image(path: str) -> str:
    """Extract text from an image via tesseract. Caller guards availability.

    Hardened against hostile images: an explicit pixel cap guards against
    decompression bombs, a wall-clock timeout stops a pathological image from
    wedging the scan, and ``O_NOFOLLOW`` refuses a symlink swapped in after the
    escape check. Any breach raises and the caller maps it to ``ocr_failed``.

    The pixel cap is enforced here rather than via ``Image.MAX_IMAGE_PIXELS``.
    That global only *warns* between 1x and 2x its value and raises above 2x, so
    setting it to N buys a real ceiling of 2N — and it is process-wide state that
    would leak this plugin's policy into every other PIL user in the host.
    ``Image.open`` reads the header without decoding, so the size is known before
    ``load()`` commits the memory.
    """
    import pytesseract
    from PIL import Image

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)  # fdopen didn't adopt the fd; close the raw one
        raise
    with fh, Image.open(fh) as img:
        width, height = img.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"image exceeds the {MAX_IMAGE_PIXELS}-pixel cap")
        img.load()  # force the decode while the fd is open
        return pytesseract.image_to_string(img, timeout=OCR_TIMEOUT_SECONDS)


# ── file classification / IO ─────────────────────────────────────────────────

def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def _read_capped(path: str) -> tuple[bytes | None, bool]:
    """Read up to the byte cap. Returns (data, truncated); (None, False) if unreadable.

    Opens with ``O_NOFOLLOW`` so a symlink swapped in after the escape check
    (TOCTOU) is refused at open time rather than followed out of the scan root.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, False
    try:
        fh = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)  # fdopen didn't adopt the fd; close the raw one
        return None, False
    try:
        with fh:
            data = fh.read(MAX_BYTES_PER_FILE + 1)
    except OSError:
        return None, False
    if len(data) > MAX_BYTES_PER_FILE:
        return data[:MAX_BYTES_PER_FILE], True
    return data, False


def _resolve_within(path: str, real_root: str) -> str | None:
    """Resolve *path*; return it if it stays within *real_root*, else None.

    Resolution of *path* happens here rather than at the call site so the symlink
    guard cannot be defeated by a caller who forgets to realpath first. Returning
    the *resolved* path (not just a bool) lets the caller open the concrete target
    instead of re-opening the walk path, so a symlink swapped in between this
    check and the open can't smuggle a read out of the scan root.

    *real_root* must already be canonical — ``scan`` derives it from a realpath'd
    target, and realpath'ing it again per file costs an lstat per path component
    for no gain. The comparison is against a canonical *resolved*, so a root that
    somehow arrived non-canonical can only fail the prefix test: this degrades to
    skipping files, never to admitting one from outside the root.
    """
    resolved = os.path.realpath(path)
    # A root of "/" already ends in a separator; "/" + os.sep would be "//" and
    # match nothing, silently skipping every file in the tree.
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    if resolved == real_root or resolved.startswith(prefix):
        return resolved
    return None


def _collect_files(real_target: str, max_files: int) -> tuple[list[str], list[tuple[str, str]], bool]:
    """Return (files, skipped_dirs, truncated). Deterministic order for CI parity.

    ``skipped_dirs`` carries ``(path, reason)`` for every directory whose contents
    were never reached. Both cases used to be silent, which let ``complete: true``
    coexist with an unscanned subtree — the one thing a gate must never claim:

    * ``os.walk(followlinks=False)`` does not descend a symlinked directory, so an
      escaping one contributed nothing to ``files`` *and* nothing to ``skipped``.
      (A symlink to a directory *inside* the root needs no entry: the walk reaches
      that content by its real path.)
    * ``os.walk``'s default ``onerror=None`` swallows the ``EACCES`` from an
      unreadable directory entirely.
    """
    if os.path.isfile(real_target):
        return [real_target], [], False

    collected: list[str] = []
    skipped_dirs: list[tuple[str, str]] = []

    def on_error(exc: OSError) -> None:
        skipped_dirs.append((getattr(exc, "filename", "") or real_target, "unreadable_directory"))

    for dirpath, dirnames, filenames in os.walk(real_target, followlinks=False, onerror=on_error):
        descend: list[str] = []
        for dn in sorted(dirnames):
            full = os.path.join(dirpath, dn)
            if os.path.islink(full) and _resolve_within(full, real_target) is None:
                skipped_dirs.append((full, "symlink_escape"))
            else:
                descend.append(dn)
        dirnames[:] = descend  # in-place: os.walk reads this back to choose its descent
        for fn in sorted(filenames):
            if len(collected) >= max_files:
                return collected, skipped_dirs, True
            collected.append(os.path.join(dirpath, fn))
    return collected, skipped_dirs, False


def _mask_pii(text: str) -> str:
    """Replace any raw PII appearing in *text* with its masked preview."""
    findings = detectors.run_detectors(text)
    if not findings:
        return text
    out: list[str] = []
    cursor = 0
    for finding in findings:  # run_detectors returns non-overlapping, start-ordered
        out.append(text[cursor:finding.start])
        out.append(finding.preview)
        cursor = finding.end
    out.append(text[cursor:])
    return "".join(out)


def _safe_error_field(value: str) -> str:
    """Render an untrusted value for an error message: masked, then bounded.

    Masking runs first. Truncating first could cut a PII value in half, and half
    an email is a raw fragment no detector would then recognise or mask.
    """
    masked = _mask_pii(value)
    if len(masked) > _MAX_ERROR_FIELD:
        return masked[:_MAX_ERROR_FIELD] + "…"
    return masked


def _safe_display_path(full: str, real_target: str, is_file_target: bool) -> str:
    """The path as shown in the result, with any PII-shaped filename masked.

    Displayed paths reach the result dict, so a file literally named after an
    email/DNI/etc. must not carry the raw value out. Masking is fused into the
    display step so no call site can render a path without it.
    """
    relative = os.path.basename(full) if is_file_target else os.path.relpath(full, real_target)
    return _mask_pii(relative)


def _display_or_placeholder(full: str, real_target: str, is_file_target: bool, fallback: str) -> str:
    """``_safe_display_path``, but never raises: if masking the name is what
    failed, no part of that name is known safe to print."""
    try:
        return _safe_display_path(full, real_target, is_file_target)
    except Exception:
        return fallback


def _extract_text(path: str, ocr_ready: Callable[[], bool]) -> tuple[str | None, str | None, bool]:
    """Pull scannable text out of *path*.

    Returns ``(text, skip_reason, truncated)``; exactly one of ``text`` and
    ``skip_reason`` is ever set. Images go through the runtime-gated OCR path,
    everything else is read as bytes and sniffed for binary content.

    *ocr_ready* is a thunk, not a bool: probing for tesseract spawns a process,
    so it must not run for a scan that never meets an image.
    """
    if _is_image(path):
        if not ocr_ready():
            return None, "ocr_unavailable", False
        try:
            return _ocr_image(path), None, False
        except Exception:
            return None, "ocr_failed", False

    data, truncated = _read_capped(path)
    if data is None:
        return None, "unreadable", False
    # Heuristic: a NUL byte in the first _NULL_SCAN_BYTES marks the file as
    # binary. This intentionally also skips UTF-16/UTF-32 text (which is full of
    # NULs); QA logs/exports are expected to be UTF-8/ASCII.
    if b"\x00" in data[:_NULL_SCAN_BYTES]:
        return None, "binary", False
    return data.decode("utf-8", errors="replace"), None, truncated


def _line_numbers(text: str, offsets: list[int]) -> list[int]:
    """1-based line numbers for *offsets*, in a single pass over *text*.

    Counting newlines from the start of the file once per offset is
    O(len(offsets) x len(text)) — 440 ms for 1000 findings in a 2 MB file, and
    the dominant cost of a scan. Visiting the offsets in ascending order and
    counting only the gap since the previous one makes the whole thing one scan.
    *offsets* need not arrive sorted; the returned list matches its order.
    """
    ascending = sorted(range(len(offsets)), key=offsets.__getitem__)
    lines = [0] * len(offsets)
    line = 1
    previous = 0
    for i in ascending:
        offset = offsets[i]
        line += text.count("\n", previous, offset)
        previous = offset
        lines[i] = line
    return lines


def _findings_for_file(
    text: str,
    types: list[str] | None,
    display: str,
    budget: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Detect PII in *text*, returning at most *budget* rows plus a capped flag."""
    found = detectors.run_detectors(text, types)
    capped = len(found) > budget
    found = found[:budget]
    if not found:
        return [], capped
    return [
        {"file": display, "line": line, "type": f.type, "preview": f.preview}
        for f, line in zip(found, _line_numbers(text, [f.start for f in found]))
    ], capped


def _target_error(target: str, real_target: str) -> dict[str, Any] | None:
    """Return a structured error if *real_target* is not a scannable path.

    The raw *target* may itself be PII (a path named after a data subject), so it
    is masked before it reaches the error string — the same invariant the display
    paths uphold: no raw value ever leaves this module in the result dict.
    """
    safe = _safe_error_field(target)
    if not os.path.exists(real_target):
        return _error(
            f"target does not exist: {safe}",
            "Pass a valid file or directory path.",
        )
    if not os.path.isfile(real_target) and not os.path.isdir(real_target):
        # Exists but is neither a regular file nor a directory (FIFO, socket,
        # device, …). Refuse rather than silently report "clean".
        return _error(
            f"target is not a regular file or directory: {safe}",
            "Pass a path to a readable file or directory.",
        )
    return None


def _validate_params(
    target: Any,
    types: Any,
    max_files: Any,
) -> dict[str, Any] | None:
    """Return a structured error, or None if every param is acceptable.

    Runs before any filesystem access — all three params are untrusted.
    """
    valid = list(detectors.DETECTOR_NAMES)

    if not isinstance(target, str) or not target.strip():
        return _error(
            "target must be a non-empty string path.",
            "Pass 'target' as a path to a file or directory.",
        )
    if len(target) > MAX_TARGET_LEN:
        # Deliberately does not echo the value: it is longer than any real path.
        return _error(
            f"target is longer than {MAX_TARGET_LEN} characters.",
            "Pass 'target' as a path to a file or directory.",
        )

    if types is not None:
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            return _error("types must be a list of detector-name strings.", _TYPES_REMEDIATION)
        if not types:
            # An explicit empty list is ambiguous (scan nothing? scan all?) and a
            # fail-open hazard for a gate — reject it rather than guess.
            return _error("types must contain at least one detector name.", _TYPES_REMEDIATION)
        if len(types) > MAX_TYPES or any(len(t) > MAX_TYPE_NAME_LEN for t in types):
            return _error(
                f"types must hold at most {MAX_TYPES} names of at most "
                f"{MAX_TYPE_NAME_LEN} characters.",
                _TYPES_REMEDIATION,
            )
        unknown = [t for t in types if t not in detectors.DETECTOR_NAMES]
        if unknown:
            # `types` is untrusted, so it is masked and bounded exactly like
            # `target`: an unknown "detector name" may be anything at all, and it
            # reaches both the result dict and the handler's log line.
            shown = ", ".join(_safe_error_field(t) for t in unknown[:3])
            if len(unknown) > 3:
                shown += f", … ({len(unknown) - 3} more)"
            return _error(
                f"unknown detector types: {shown}",
                f"Valid detectors: {valid}.",
            )

    if max_files is not None:
        if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files <= 0:
            return _error(
                "max_files must be a positive integer.",
                "Omit 'max_files' or pass a positive integer file cap.",
            )
        if max_files > MAX_MAX_FILES:
            # A bound the caller can raise is not a bound; the caller is a model.
            return _error(
                f"max_files must be at most {MAX_MAX_FILES}.",
                f"Omit 'max_files' (default {DEFAULT_MAX_FILES}) or pass a smaller cap.",
            )

    return None


# ── public entry point ───────────────────────────────────────────────────────

def scan(
    target: Any,
    types: list[str] | None = None,
    max_files: int | None = None,
) -> dict[str, Any]:
    """Scan *target* for residual PII and return the masked result contract.

    All arguments are treated as untrusted and validated before any filesystem
    access. Returns a plain dict (the tool handler JSON-encodes it).
    """
    invalid = _validate_params(target, types, max_files)
    if invalid is not None:
        return invalid

    # -- resolve target (realpath; no symlink escaping the scan root) ----------
    real_target = os.path.realpath(target)
    unscannable = _target_error(target, real_target)
    if unscannable is not None:
        return unscannable

    is_file_target = os.path.isfile(real_target)
    effective_max = max_files if max_files is not None else DEFAULT_MAX_FILES

    files, skipped_dirs, files_truncated = _collect_files(real_target, effective_max)

    scanned = 0
    findings: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [
        {
            "file": _display_or_placeholder(path, real_target, is_file_target, f"<directory #{i}>"),
            "reason": reason,
        }
        for i, (path, reason) in enumerate(skipped_dirs)
    ]
    # Probed at most once per scan, and only once the walk actually reaches an
    # image — `_ocr_available` shells out to `tesseract --version` via pytesseract.
    ocr_ready = cache(_ocr_available)
    bytes_truncated = False
    findings_capped = False
    timed_out = False
    deadline = time.monotonic() + MAX_SCAN_SECONDS

    for index, full in enumerate(files):
        # Nothing else bounds the loop's wall clock: `max_files` x
        # `MAX_BYTES_PER_FILE` is 20 GB of text through 8 detectors, and only the
        # OCR call has a timeout of its own.
        if time.monotonic() > deadline:
            timed_out = True
            break
        # A PII-free stand-in, used if masking the path is itself what fails: at
        # that point no part of the name is known safe to put in the result.
        display = f"<file #{index}>"
        try:
            display = _safe_display_path(full, real_target, is_file_target)

            # Symlink-escape guard (directory walk only; an explicitly targeted
            # file was chosen by the caller). Resolve once and open the *resolved*
            # target rather than the walk path, so a symlink swapped in after this
            # check can't be followed out of the scan root.
            if is_file_target:
                safe_path = full
            else:
                safe_path = _resolve_within(full, real_target)
                if safe_path is None:
                    skipped.append({"file": display, "reason": "symlink_escape"})
                    continue

            # Only read regular files. Opening a FIFO/socket/device (or a broken
            # symlink) for reading could block the scan indefinitely.
            if not os.path.isfile(safe_path):
                skipped.append({"file": display, "reason": "not_regular_file"})
                continue

            text, skip_reason, file_truncated = _extract_text(safe_path, ocr_ready)
            if skip_reason is not None:
                skipped.append({"file": display, "reason": skip_reason})
                continue
            # Recorded as soon as it is known, not after the detectors run: a cap
            # was hit whether or not the scan of this file goes on to fail, and
            # `truncated` means exactly "some bytes in scope were never scanned".
            bytes_truncated = bytes_truncated or file_truncated

            rows, findings_capped = _findings_for_file(
                text, types, display, MAX_FINDINGS - len(findings)
            )
        except Exception:
            # One hostile or pathological file must not sink the gate: without
            # this, a raise on file N discards the findings of files 1..N-1 and
            # the caller sees a bare "scan failed". Degrading to a skip keeps
            # those findings and, because `skipped` is non-empty, forces
            # `complete: false` — so a caller gating on `clean and complete`
            # still refuses to attach the evidence. The exception is deliberately
            # not logged: its message could quote the raw bytes that caused it.
            skipped.append({"file": display, "reason": "scan_failed"})
            continue

        scanned += 1
        findings.extend(rows)
        if findings_capped:
            break

    truncated = files_truncated or bytes_truncated or findings_capped or timed_out
    clean = not findings
    # A gate must not let "clean" read as "safe to attach" when coverage was
    # incomplete: any skipped file (binary, image without OCR, symlink escape,
    # unreadable, …) or any truncation means some bytes were never scanned.
    # `complete` exposes that so callers gate on `clean and complete`.
    complete = not truncated and not skipped
    result: dict[str, Any] = {
        "success": True,
        "clean": clean,
        "complete": complete,
        "scanned_files": scanned,
        "findings": findings,
        "skipped": skipped,
        "truncated": truncated,
    }
    if not clean:
        result["summary"] = dict(Counter(f["type"] for f in findings))
    return result
