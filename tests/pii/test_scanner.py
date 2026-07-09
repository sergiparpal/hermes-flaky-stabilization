"""Phase 3 — end-to-end scanning against a real tmp_path tree.

Exercises the 4.3 output contract: dirty file → findings, clean file → clean,
missing path → structured error, masked previews, and the max_files bound.
(OCR and security-hardening tests live further down, added in Phases 4 and 5.)
"""
import json

import pytest
import scanner

# ── dirty / clean / missing ──────────────────────────────────────────────────

def test_dirty_file_reports_masked_findings(tmp_path):
    f = tmp_path / "app.log"
    f.write_text(
        "line 1 nothing here\n"
        "user email alice@example.com logged in\n"
        "charged card 4111 1111 1111 1111 ok\n"
    )
    res = scanner.scan(str(f))

    assert res["success"] is True
    assert res["clean"] is False
    assert res["scanned_files"] == 1
    types = {x["type"] for x in res["findings"]}
    assert types == {"email", "credit_card"}
    assert res["summary"] == {"email": 1, "credit_card": 1}

    by_type = {x["type"]: x for x in res["findings"]}
    assert by_type["email"]["line"] == 2
    assert by_type["credit_card"]["line"] == 3
    # previews are masked; raw values never appear
    assert by_type["credit_card"]["preview"] == "************1111"
    assert "alice@example.com" not in json.dumps(res)
    assert "4111111111111111" not in json.dumps(res)


def test_clean_file(tmp_path):
    f = tmp_path / "clean.log"
    f.write_text("nothing sensitive here\njust some ordinary text\n")
    res = scanner.scan(str(f))
    assert res == {
        "success": True,
        "clean": True,
        "complete": True,
        "scanned_files": 1,
        "findings": [],
        "skipped": [],
        "truncated": False,
    }


def test_missing_path_structured_error(tmp_path):
    res = scanner.scan(str(tmp_path / "does_not_exist"))
    assert res["success"] is False
    assert "does not exist" in res["error"]
    assert res["remediation"]


def test_empty_or_bad_target():
    for bad in [None, "", "   ", 123, []]:
        res = scanner.scan(bad)
        assert res["success"] is False
        assert res["remediation"]


# ── directory walk ───────────────────────────────────────────────────────────

def test_directory_walk_relative_paths(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("mail bob@corp.io here")
    (tmp_path / "sub" / "b.txt").write_text("clean")
    res = scanner.scan(str(tmp_path))
    assert res["scanned_files"] == 2
    assert res["findings"][0]["file"] == "a.txt"
    assert res["findings"][0]["type"] == "email"


# ── bounds ───────────────────────────────────────────────────────────────────

def test_max_files_bound(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("mail x@y.com")
    res = scanner.scan(str(tmp_path), max_files=2)
    assert res["scanned_files"] <= 2
    assert res["truncated"] is True


def test_bad_max_files_rejected(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    for bad in [0, -1, 2.5, True, "3"]:
        res = scanner.scan(str(tmp_path), max_files=bad)
        assert res["success"] is False


# ── type subset + unknown types ──────────────────────────────────────────────

def test_types_subset(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("mail alice@example.com card 4111 1111 1111 1111")
    res = scanner.scan(str(f), types=["email"])
    assert {x["type"] for x in res["findings"]} == {"email"}


def test_unknown_type_rejected(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("hi")
    res = scanner.scan(str(f), types=["email", "bogus_type"])
    assert res["success"] is False
    assert "bogus_type" in res["error"]


# ── binary handling ──────────────────────────────────────────────────────────

def test_binary_file_skipped(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"PK\x03\x04\x00\x00 binary \x00 alice@example.com")
    res = scanner.scan(str(f))
    assert res["scanned_files"] == 0
    assert res["skipped"] == [{"file": "blob.bin", "reason": "binary"}]
    # even if an email-shaped run is in the bytes, a binary file is not scanned
    assert res["clean"] is True


# ── handler wiring returns a JSON string ─────────────────────────────────────

def _load_plugin_init():
    """The stage package owning validate_no_pii (unified-plugin adaptation:
    the legacy repo-root __init__.py became hermes_flaky_stabilization.pii)."""
    import importlib
    return importlib.import_module("hermes_flaky_stabilization.pii")


def test_handler_returns_json_string(tmp_path):
    mod = _load_plugin_init()
    f = tmp_path / "x.log"
    f.write_text("mail alice@example.com")
    out = mod._handle_validate_no_pii({"target": str(f)})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["success"] is True and parsed["clean"] is False


def test_handler_rejects_non_dict_params():
    mod = _load_plugin_init()
    out = mod._handle_validate_no_pii("not a dict")
    parsed = json.loads(out)
    assert parsed["success"] is False and parsed["remediation"]


# ── Phase 4: OCR path (runtime-gated) ─────────────────────────────────────────

def test_image_skipped_when_ocr_unavailable(tmp_path, monkeypatch):
    """With no tesseract, an image is reported as skipped, not scanned; text
    files in the same tree are still scanned normally."""
    monkeypatch.setattr(scanner, "_ocr_available", lambda: False)
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n not a real image")
    (tmp_path / "notes.txt").write_text("mail alice@example.com")

    res = scanner.scan(str(tmp_path))
    assert res["scanned_files"] == 1  # the .txt, not the image
    assert {"file": "shot.png", "reason": "ocr_unavailable"} in res["skipped"]
    assert any(f["type"] == "email" for f in res["findings"])


def test_image_ocr_detects_pii_when_available(tmp_path, monkeypatch):
    """When OCR is available, image text runs through the same detectors."""
    monkeypatch.setattr(scanner, "_ocr_available", lambda: True)
    monkeypatch.setattr(
        scanner, "_ocr_image",
        lambda path: "receipt card 4111 1111 1111 1111 thanks",
    )
    (tmp_path / "receipt.jpg").write_bytes(b"jpeg-ish bytes")

    res = scanner.scan(str(tmp_path))
    assert res["scanned_files"] == 1
    assert res["skipped"] == []
    assert res["findings"][0]["type"] == "credit_card"
    assert res["findings"][0]["preview"] == "************1111"


def test_image_ocr_failure_is_skipped_not_fatal(tmp_path, monkeypatch):
    def _boom(path):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(scanner, "_ocr_available", lambda: True)
    monkeypatch.setattr(scanner, "_ocr_image", _boom)
    (tmp_path / "x.png").write_bytes(b"bytes")
    (tmp_path / "y.txt").write_text("clean")

    res = scanner.scan(str(tmp_path))
    assert res["success"] is True
    assert {"file": "x.png", "reason": "ocr_failed"} in res["skipped"]


def test_real_ocr_end_to_end(tmp_path):
    """Only runs where pytesseract + a tesseract binary are actually present."""
    pytesseract = pytest.importorskip("pytesseract")
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        pytest.skip("tesseract binary not installed")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1000, 140), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    draw.text((20, 40), "email test@example.com", fill="black", font=font)
    png = tmp_path / "render.png"
    img.save(png)

    res = scanner.scan(str(png))
    assert res["success"] is True
    assert res["scanned_files"] == 1
    assert res["skipped"] == []


# ── Phase 5: hardening (security) ─────────────────────────────────────────────

import logging
import os


def test_symlink_escape_is_refused(tmp_path):
    """A symlink pointing outside the scan root is skipped, never followed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("leaked card 4111 1111 1111 1111")

    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("clean data")
    os.symlink(secret, root / "link.txt")

    res = scanner.scan(str(root))
    assert {"file": "link.txt", "reason": "symlink_escape"} in res["skipped"]
    assert res["clean"] is True  # the escaping symlink's PII was never read
    assert res["scanned_files"] == 1  # only ok.txt


def test_symlink_within_root_is_allowed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "data.txt").write_text("mail alice@example.com")
    os.symlink(root / "data.txt", root / "alias.txt")
    res = scanner.scan(str(root))
    assert not any(s["reason"] == "symlink_escape" for s in res["skipped"])


def test_scan_performs_no_filesystem_writes(tmp_path):
    (tmp_path / "a.txt").write_text("mail alice@example.com")
    (tmp_path / "b.log").write_text("card 4111 1111 1111 1111")

    def snapshot():
        snap = {}
        for dp, _dn, fns in os.walk(tmp_path):
            for fn in fns:
                p = os.path.join(dp, fn)
                st = os.stat(p)
                snap[p] = (st.st_size, st.st_mtime_ns)
        return snap

    before = snapshot()
    scanner.scan(str(tmp_path))
    after = snapshot()
    assert before == after, "scan must not create, modify, or remove any file"


def test_logging_never_emits_raw_pii(tmp_path, caplog):
    mod = _load_plugin_init()
    f = tmp_path / "app.log"
    f.write_text("mail alice@example.com card 4111 1111 1111 1111")

    with caplog.at_level(logging.INFO):
        out = mod._handle_validate_no_pii({"target": str(f)})

    # The handler logged something (types/counts), but no raw PII.
    assert "validate_no_pii" in caplog.text
    assert "alice@example.com" not in caplog.text
    assert "4111111111111111" not in caplog.text
    assert "4111 1111 1111 1111" not in caplog.text
    # ...and the masked preview is never logged either.
    assert "************1111" not in caplog.text
    # sanity: the JSON result itself carries only masked previews
    assert "alice@example.com" not in out
    assert "4111111111111111" not in out


def test_findings_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "MAX_FINDINGS", 3)
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"user{i}@example.com" for i in range(10)))
    res = scanner.scan(str(f))
    assert len(res["findings"]) == 3
    assert res["truncated"] is True


def test_per_file_byte_cap_truncates(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "MAX_BYTES_PER_FILE", 16)
    f = tmp_path / "big.txt"
    # PII sits beyond the byte cap, so it must not be read/reported.
    f.write_text(("padding " * 4) + "alice@example.com")
    res = scanner.scan(str(f))
    assert res["truncated"] is True
    assert res["clean"] is True


def test_all_error_paths_carry_remediation(tmp_path):
    cases = [
        (None, None, None),
        ("", None, None),
        (str(tmp_path / "nope"), None, None),
        (str(tmp_path), ["not_a_detector"], None),
        (str(tmp_path), None, -5),
    ]
    for target, types, max_files in cases:
        res = scanner.scan(target, types, max_files)
        assert res["success"] is False
        assert res.get("error")
        assert res.get("remediation")


def test_fifo_in_directory_is_skipped_not_hung(tmp_path):
    """A FIFO in the tree must be skipped as an irregular file, never opened —
    opening it for reading would block the scan indefinitely."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo not available on this platform")
    import threading

    (tmp_path / "real.txt").write_text("mail alice@example.com")
    os.mkfifo(tmp_path / "pipe.log")

    box = {}
    t = threading.Thread(target=lambda: box.update(res=scanner.scan(str(tmp_path))), daemon=True)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "scan hung on a FIFO (irregular files must be skipped)"

    res = box["res"]
    assert res["success"] is True
    assert res["scanned_files"] == 1
    assert {"file": "pipe.log", "reason": "not_regular_file"} in res["skipped"]


def test_directly_targeted_special_file_errors(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo not available on this platform")
    fifo = tmp_path / "p"
    os.mkfifo(fifo)
    res = scanner.scan(str(fifo))
    assert res["success"] is False
    assert res["remediation"]


def test_pii_shaped_filename_is_masked_in_output(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "alice@example.com.log").write_text("card 4111 1111 1111 1111")
    res = scanner.scan(str(d))
    dump = json.dumps(res)
    assert "alice@example.com" not in dump  # raw PII filename never leaks
    assert res["findings"][0]["file"] != "alice@example.com.log"


def test_empty_types_list_rejected(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    res = scanner.scan(str(tmp_path), types=[])
    assert res["success"] is False
    assert res["remediation"]


def test_source_has_no_shell_or_dynamic_exec():
    """§3.11: no exec/eval and no subprocess/os.system in the PII stage source.

    (The package-wide sweep with its pinned subprocess allowlist lives in
    tests/test_security_scan.py; this keeps the legacy per-stage guard.)"""
    import hermes_flaky_stabilization.pii as pii_pkg
    root = os.path.dirname(os.path.abspath(pii_pkg.__file__))
    forbidden = ["subprocess", "os.system(", "eval(", "exec(", "os.popen("]
    for name in ["__init__.py", "detectors.py", "scanner.py", "redaction.py"]:
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            src = fh.read()
        for token in forbidden:
            assert token not in src, f"{name} contains forbidden call: {token}"


# ── security regressions (audit fixes) ────────────────────────────────────────

def test_email_detector_is_not_quadratic_on_large_blob(tmp_path):
    """A long run of local-part characters with no '@' (base64url / JWT / token
    blobs) must not trigger quadratic backtracking in the email regex. Runs in a
    watchdog thread so a regression fails fast instead of hanging the suite."""
    import threading

    (tmp_path / "tokens.log").write_text("abcXYZ012_-." * 90_000)  # ~1 MB, all local-class

    box = {}
    t = threading.Thread(
        target=lambda: box.update(res=scanner.scan(str(tmp_path))), daemon=True
    )
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "email detector went quadratic on a large token blob"
    assert box["res"]["success"] is True
    assert box["res"]["clean"] is True  # a bare token blob holds no valid email


def test_missing_target_path_does_not_leak_raw_pii(tmp_path):
    """A non-existent target whose path is itself PII must be masked in the error
    (the raw value must never reach the result dict)."""
    bad = str(tmp_path / "alice@example.com" / "card-4111111111111111.log")
    res = scanner.scan(bad)
    assert res["success"] is False
    dump = json.dumps(res)
    assert "alice@example.com" not in dump
    assert "4111111111111111" not in dump
    assert res["remediation"]


def test_complete_flag_reflects_coverage(tmp_path):
    """`complete` is True only when nothing was skipped or truncated — so a caller
    can require `clean and complete` before attaching evidence."""
    (tmp_path / "a.txt").write_text("nothing sensitive")
    res = scanner.scan(str(tmp_path))
    assert res["clean"] is True and res["complete"] is True

    # A skipped (binary) file leaves no findings but means coverage is incomplete.
    (tmp_path / "b.bin").write_bytes(b"\x00\x00 binary blob")
    res = scanner.scan(str(tmp_path))
    assert res["clean"] is True
    assert res["complete"] is False
    assert any(s["reason"] == "binary" for s in res["skipped"])


def test_truncation_marks_scan_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "MAX_BYTES_PER_FILE", 16)
    (tmp_path / "big.txt").write_text("padding " * 8)
    res = scanner.scan(str(tmp_path))
    assert res["truncated"] is True
    assert res["complete"] is False


def test_failing_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    """One pathological file must not sink the whole gate.

    Before the per-file guard, a raise while scanning file N propagated out of
    ``scan()`` and discarded the findings of files 1..N-1; the caller saw a bare
    "scan failed" with no partial coverage and no idea which file broke.
    """
    import detectors
    real = detectors.run_detectors

    def exploding(text, types=None):
        if "BOOM" in text:
            raise RuntimeError("detector blew up on 4111111111111111")
        return real(text, types)

    monkeypatch.setattr(detectors, "run_detectors", exploding)
    (tmp_path / "a.txt").write_text("mail alice@example.com")
    (tmp_path / "boom.txt").write_text("BOOM payload")
    (tmp_path / "c.txt").write_text("card 4111 1111 1111 1111")

    res = scanner.scan(str(tmp_path))
    assert res["success"] is True
    assert {"file": "boom.txt", "reason": "scan_failed"} in res["skipped"]
    # The failure is contained: files before *and* after it are still scanned.
    assert {f["type"] for f in res["findings"]} == {"email", "credit_card"}
    assert res["scanned_files"] == 2
    # A skipped file means incomplete coverage, so `clean and complete` still fails.
    assert res["complete"] is False
    # The exception's own message quoted raw PII; it must not reach the result.
    assert "4111111111111111" not in json.dumps(res)


def test_unmaskable_filename_falls_back_to_positional_id(tmp_path, monkeypatch):
    """If masking the *path* is what fails, no part of the name is known safe."""
    import detectors
    real = detectors.run_detectors

    def exploding(text, types=None):
        if "zzz" in text:
            raise RuntimeError("boom")
        return real(text, types)

    monkeypatch.setattr(detectors, "run_detectors", exploding)
    (tmp_path / "zzz.txt").write_text("clean")

    res = scanner.scan(str(tmp_path))
    assert res["skipped"] == [{"file": "<file #0>", "reason": "scan_failed"}]
    assert "zzz" not in json.dumps(res)
    assert res["complete"] is False


# ── coverage gaps: `complete` must never claim a subtree that was not scanned ──

def test_escaping_directory_symlink_is_reported(tmp_path):
    """os.walk(followlinks=False) silently declines to descend a symlinked dir.

    Unreported, that left `clean: true, complete: true` while an entire subtree
    holding a card was never opened — the gate's worst possible answer.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("card 4111 1111 1111 1111")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("clean data")
    os.symlink(outside, root / "linkdir")

    res = scanner.scan(str(root))
    assert {"file": "linkdir", "reason": "symlink_escape"} in res["skipped"]
    assert res["clean"] is True          # its contents were never read
    assert res["complete"] is False      # ...and the caller is told so
    assert res["scanned_files"] == 1


def test_within_root_directory_symlink_needs_no_skip_entry(tmp_path):
    """Its target is reached by its real path, so coverage is genuinely complete."""
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real"
    real.mkdir()
    (real / "a.txt").write_text("mail alice@example.com")
    os.symlink(real, root / "alias")

    res = scanner.scan(str(root))
    assert res["skipped"] == []
    assert res["complete"] is True
    assert any(f["type"] == "email" for f in res["findings"])


def test_unreadable_directory_is_reported(tmp_path):
    """os.walk's default onerror=None swallows EACCES entirely."""
    if os.geteuid() == 0:
        pytest.skip("root can read a 0o000 directory")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("clean")
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("card 4111 1111 1111 1111")
    os.chmod(locked, 0o000)
    try:
        res = scanner.scan(str(root))
    finally:
        os.chmod(locked, 0o700)  # let tmp_path cleanup succeed

    assert any(s["reason"] == "unreadable_directory" for s in res["skipped"])
    assert res["clean"] is True
    assert res["complete"] is False


# ── bounded, masked error strings ─────────────────────────────────────────────

def test_unknown_types_are_masked_in_the_error(tmp_path):
    """`types` is untrusted and reaches both the result dict and the log line."""
    res = scanner.scan(str(tmp_path), types=["alice@example.com"])
    assert res["success"] is False
    assert "alice@example.com" not in json.dumps(res)
    assert res["remediation"]


def test_error_strings_are_bounded(tmp_path):
    # An over-long target/type name is rejected without being echoed at all.
    res = scanner.scan("/nonexistent/" + "p" * 200_000)
    assert res["success"] is False and len(res["error"]) < 200

    res = scanner.scan(str(tmp_path), types=["x" * 50_000])
    assert res["success"] is False and len(res["error"]) < 200

    res = scanner.scan(str(tmp_path), types=["nope"] * 100)
    assert res["success"] is False and len(res["error"]) < 200

    # A plausible-length unknown type is quoted, but masked and truncated.
    res = scanner.scan(str(tmp_path), types=["z" * 60])
    assert res["success"] is False
    assert len(res["error"]) < 200


def test_max_files_ceiling_cannot_be_raised_by_the_caller(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    res = scanner.scan(str(tmp_path), max_files=scanner.MAX_MAX_FILES + 1)
    assert res["success"] is False
    assert res["remediation"]
    # ...but a value at the ceiling is fine.
    assert scanner.scan(str(tmp_path), max_files=scanner.MAX_MAX_FILES)["success"] is True


def test_scan_is_wall_clock_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "MAX_SCAN_SECONDS", -1)  # deadline already passed
    (tmp_path / "a.txt").write_text("mail alice@example.com")
    res = scanner.scan(str(tmp_path))
    assert res["success"] is True
    assert res["scanned_files"] == 0
    assert res["truncated"] is True and res["complete"] is False


def test_resolve_within_handles_a_root_ending_in_a_separator():
    # "/" + os.sep == "//" would match nothing and skip every file in the tree.
    assert scanner._resolve_within("/etc", "/") == "/etc"
    assert scanner._resolve_within("/etc", "/etc") == "/etc"


def test_symlink_within_root_content_is_scanned(tmp_path):
    """The resolved-target open (O_NOFOLLOW on the real file) still reads content
    reached through an allowed within-root symlink."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "data.txt").write_text("mail alice@example.com")
    os.symlink(root / "data.txt", root / "alias.txt")
    res = scanner.scan(str(root))
    assert any(f["type"] == "email" for f in res["findings"])
    assert not any(s["reason"] == "symlink_escape" for s in res["skipped"])
