"""Tests for the improve_bug_report handler. No real LLM is ever called."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hermes_bug_report_improver import (
    domain,
    engine,
    handler,
    prompts,
    rendering,
    schema,
    validation,
)

EXAMPLE_A = prompts.FEW_SHOT_EXAMPLES[0]["output"]  # vague   -> unknown
EXAMPLE_B = prompts.FEW_SHOT_EXAMPLES[1]["output"]  # detailed-> high
EXAMPLE_C = prompts.FEW_SHOT_EXAMPLES[2]["output"]  # two bugs-> low + flag


def _run(ctx, **args):
    args.setdefault("raw_text", "something is broken")
    return handler.make_handler(ctx)(args)


def _run_json(ctx, **args):
    """Run the handler and parse its JSON output (success envelope or error)."""
    return json.loads(_run(ctx, **args))


# --- input validation ----------------------------------------------------------
def test_empty_input_returns_error(mock_ctx):
    out = _run_json(mock_ctx(), raw_text="")
    assert "error" in out


def test_missing_raw_text_returns_error(mock_ctx):
    out = json.loads(handler.make_handler(mock_ctx())({}))
    assert "error" in out


def test_non_dict_args_returns_error(mock_ctx):
    out = json.loads(handler.make_handler(mock_ctx())("not a dict"))
    assert "error" in out


def test_oversized_input_returns_error(mock_ctx):
    big = "x" * (schema.MAX_RAW_TEXT_BYTES + 1)
    out = _run_json(mock_ctx(), raw_text=big)
    assert "error" in out and "KB" in out["error"]


def test_oversized_context_returns_error(mock_ctx):
    big = "x" * (schema.MAX_CONTEXT_BYTES + 1)
    out = _run_json(mock_ctx(), context=big)
    assert "error" in out and "KB" in out["error"]


def test_invalid_format_returns_error(mock_ctx):
    out = _run_json(mock_ctx(), format="xml")
    assert "error" in out


def test_non_string_context_returns_error(mock_ctx):
    out = _run_json(mock_ctx(), context=123)
    assert "error" in out


def test_falsy_non_string_context_returns_error(mock_ctx):
    # Falsy non-strings ([], {}, 0, False) are rejected, not coerced to "".
    out = _run_json(mock_ctx(), context=[])
    assert "error" in out


def test_non_string_format_returns_error(mock_ctx):
    out = _run_json(mock_ctx(), format=123)
    assert "error" in out


def test_empty_format_defaults_to_markdown(mock_ctx):
    assert _run(mock_ctx([EXAMPLE_B]), format="").startswith("# ")


# --- the three canonical examples ----------------------------------------------
def test_vague_input_lists_missing_evidence(mock_ctx):
    out = _run_json(mock_ctx([EXAMPLE_A]), raw_text="login broken sometimes", format="json")
    assert out["severity"] == "unknown"
    assert out["reproduction_steps"] == []
    assert len(out["missing_evidence"]) >= 4


def test_detailed_input_returns_high_severity(mock_ctx):
    out = _run_json(mock_ctx([EXAMPLE_B]), format="json")
    assert out["severity"] == "high"
    assert out["missing_evidence"] == []


def test_multi_bug_input_flags_extras(mock_ctx):
    out = _run_json(mock_ctx([EXAMPLE_C]), format="json")
    assert any(
        ("separate" in e.lower()) or ("second" in e.lower()) for e in out["missing_evidence"]
    )


# --- formatting ----------------------------------------------------------------
def test_markdown_format_renders_headings(mock_ctx):
    md = _run(mock_ctx([EXAMPLE_B]), format="markdown")
    assert md.startswith("# ")
    for heading in (
        "## Reproduction Steps",
        "## Expected Behavior",
        "## Actual Behavior",
        "## Missing Evidence",
    ):
        assert heading in md
    assert "## Severity: high" in md


def test_default_format_is_markdown(mock_ctx):
    assert _run(mock_ctx([EXAMPLE_B])).startswith("# ")


def test_json_format_returns_valid_json(mock_ctx):
    obj = _run_json(mock_ctx([EXAMPLE_A]), format="json")
    assert set(obj) == set(schema.REQUIRED_OUTPUT_FIELDS)


def test_markdown_handles_empty_steps_and_evidence(mock_ctx):
    md = _run(mock_ctx([EXAMPLE_A]), format="markdown")  # A: no steps
    assert "_None provided._" in md  # empty reproduction_steps
    md_b = _run(mock_ctx([EXAMPLE_B]), format="markdown")  # B: no missing evidence
    assert "_None — the report appears complete._" in md_b


def test_markdown_empty_summary_and_rationale_use_fallback(mock_ctx):
    sparse = dict(EXAMPLE_B, summary="", severity_rationale="")
    md = _run(mock_ctx([sparse]), format="markdown")
    assert md.count("_Not provided._") >= 2  # summary slot + rationale slot


# --- output sanitization (untrusted model text in Markdown) --------------------
def test_markdown_html_escapes_model_text(mock_ctx):
    evil = dict(EXAMPLE_B, summary="<img src=x onerror=alert(1)>")
    md = _run(mock_ctx([evil]), format="markdown")
    assert "<img" not in md
    assert "&lt;img" in md


def test_markdown_strips_ansi_escape_sequences(mock_ctx):
    evil = dict(EXAMPLE_B, actual_behavior="boom\x1b[31mRED\x1b[0m")
    md = _run(mock_ctx([evil]), format="markdown")
    assert "\x1b" not in md


def test_markdown_collapses_newlines_to_prevent_forged_blocks(mock_ctx):
    evil = dict(EXAMPLE_B, summary="legit\n## Severity: critical\nmore")
    md = _run(mock_ctx([evil]), format="markdown")
    assert "\n## Severity: critical" not in md  # forged heading neutralized
    assert "## Severity: high" in md  # the real severity heading is intact


def test_markdown_strips_bidi_override(mock_ctx):
    rlo = chr(0x202E)  # U+202E RIGHT-TO-LEFT OVERRIDE — a "Trojan Source" spoof char
    evil = dict(EXAMPLE_B, title=f"safe{rlo}malicious")
    md = _run(mock_ctx([evil]), format="markdown")
    assert rlo not in md


def test_json_format_is_byte_faithful(mock_ctx):
    # The json path returns exact text (no HTML-escaping); structural safety is
    # the encoder's job, display escaping is the consumer's.
    evil = dict(EXAMPLE_B, summary="a < b & c")
    out = _run_json(mock_ctx([evil]), format="json")
    assert out["summary"] == "a < b & c"


# --- leading block-marker escaping (a field rendered at a block boundary) -------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("# h", "\\# h"),
        ("## Severity: critical", "\\## Severity: critical"),
        ("- item", "\\- item"),
        ("* item", "\\* item"),
        ("+ item", "\\+ item"),
        ("| a | b |", "\\| a | b |"),
        ("```", "\\" + "```"),  # code fence
        ("~~~", "\\~~~"),  # code fence (tilde)
        ("1. step", "1\\. step"),  # ordered list: escape the delimiter
        ("1) step", "1\\) step"),
        ("12. step", "12\\. step"),  # multi-digit marker
    ],
)
def test_sanitize_escapes_leading_block_markers(raw, expected):
    # A field whose entire (whitespace-collapsed) content begins with a block
    # marker would otherwise forge that block, since each field is rendered at a
    # block boundary. Only the leading marker is escaped.
    assert rendering._sanitize_for_md(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "plain text",
        "3.14 is the ratio",  # decimal -> not an ordered-list marker
        "v1.2.3 was released",  # version string
        "uses - and # mid-text",  # markers only mid-text
        "> quoted",  # leading > is HTML-escaped to &gt;, not backslashed
    ],
)
def test_sanitize_does_not_overescape_legitimate_text(raw):
    assert "\\" not in rendering._sanitize_for_md(raw)


def test_sanitize_empty_or_blank_is_not_escaped():
    # Guard the empty-prefix edge: "" must not become a lone backslash.
    assert rendering._sanitize_for_md("") == ""
    assert rendering._sanitize_for_md("   ") == ""  # whitespace collapses away
    assert rendering._sanitize_for_md("\x1b\x1b") == ""  # control chars stripped away


def test_markdown_neutralizes_field_that_is_itself_a_heading(mock_ctx):
    # End-to-end: a summary that is itself a heading must not render as a heading
    # block (it would spoof a section); the real, validated severity stays intact.
    evil = dict(EXAMPLE_B, summary="## Severity: critical")
    md = _run(mock_ctx([evil]), format="markdown")
    assert not any(line.startswith("## Severity: critical") for line in md.splitlines())
    assert "## Severity: high" in md


def test_markdown_neutralizes_field_that_is_a_code_fence(mock_ctx):
    # End-to-end: a lone code fence must not open a block that swallows the rest
    # of the report (the real Severity / Missing Evidence sections).
    evil = dict(EXAMPLE_B, actual_behavior="```")
    md = _run(mock_ctx([evil]), format="markdown")
    assert not any(line.strip() == "```" for line in md.splitlines())
    assert "## Severity: high" in md
    assert "## Missing Evidence" in md


# --- call wiring ---------------------------------------------------------------
def test_context_is_passed_to_model(mock_ctx):
    ctx = mock_ctx([EXAMPLE_B])
    _run(ctx, raw_text="boom", context="macOS 14.4")
    assert "macOS 14.4" in ctx.llm.calls[0]["input"][0]["text"]


def test_output_schema_is_enforced_by_host(mock_ctx):
    ctx = mock_ctx([EXAMPLE_B])
    _run(ctx, format="json")
    call = ctx.llm.calls[0]
    assert call["json_schema"] is schema.BUG_REPORT_OUTPUT_SCHEMA
    assert call["temperature"] == 0.0
    assert "Worked examples" in call["instructions"]


def test_handler_tolerates_kwargs_invocation(mock_ctx):
    ctx = mock_ctx([EXAMPLE_B])
    out = handler.make_handler(ctx)({"raw_text": "x", "format": "json"}, task_id="t1", extra="ignored")
    assert json.loads(out)["severity"] == "high"


# --- retry logic ---------------------------------------------------------------
def test_invalid_llm_response_retries_once(mock_ctx):
    ctx = mock_ctx([None, EXAMPLE_B])  # unparseable, then good
    out = _run_json(ctx, format="json")
    assert out["severity"] == "high"
    assert len(ctx.llm.calls) == 2
    assert engine._RETRY_SUFFIX in ctx.llm.calls[1]["instructions"]


def test_invalid_llm_response_returns_error_on_second_failure(mock_ctx):
    ctx = mock_ctx([None, None])
    out = _run_json(ctx)
    assert "error" in out
    assert len(ctx.llm.calls) == 2  # exactly one retry, no more


def test_structurally_invalid_output_triggers_retry(mock_ctx):
    bad = dict(EXAMPLE_B, severity="catastrophic")  # invalid severity
    ctx = mock_ctx([bad, EXAMPLE_B])
    out = _run_json(ctx, format="json")
    assert out["severity"] == "high"
    assert len(ctx.llm.calls) == 2


def test_host_value_error_triggers_retry(mock_ctx):
    # A host enforcing the schema (optional jsonschema) raises ValueError on a
    # violation; that is retryable, not surfaced immediately.
    ctx = mock_ctx([ValueError("output did not match schema"), EXAMPLE_B])
    out = _run_json(ctx, format="json")
    assert out["severity"] == "high"
    assert len(ctx.llm.calls) == 2
    assert engine._RETRY_SUFFIX in ctx.llm.calls[1]["instructions"]


def test_host_value_error_on_both_attempts_returns_error(mock_ctx):
    ctx = mock_ctx([ValueError("bad"), ValueError("bad again")])
    out = _run_json(ctx)
    assert "error" in out
    assert len(ctx.llm.calls) == 2  # exactly one retry, then give up


# --- failure modes -------------------------------------------------------------
def test_llm_exception_returns_error(mock_ctx):
    out = _run_json(mock_ctx([RuntimeError("secret-internal-detail")]))
    assert "error" in out
    # Raw exception text must not be reflected to the caller (it is logged).
    assert "secret-internal-detail" not in out["error"]


def test_llm_unavailable_returns_error(no_llm_ctx):
    out = _run_json(no_llm_ctx)
    assert "error" in out and "unavailable" in out["error"].lower()


# --- domain coercion unit tests (ImprovedBugReport.from_parsed) -----------------
def test_schema_validates_required_fields():
    incomplete = {k: v for k, v in EXAMPLE_B.items() if k != "title"}
    with pytest.raises(ValueError):
        domain.ImprovedBugReport.from_parsed(incomplete)


def test_coerce_rejects_bad_severity():
    with pytest.raises(ValueError):
        domain.ImprovedBugReport.from_parsed(dict(EXAMPLE_B, severity="nope"))


def test_coerce_rejects_non_array_steps():
    with pytest.raises(ValueError):
        domain.ImprovedBugReport.from_parsed(dict(EXAMPLE_B, reproduction_steps="1. do x"))


def test_coerce_rejects_non_dict():
    with pytest.raises(ValueError):
        domain.ImprovedBugReport.from_parsed(["not", "a", "dict"])


def test_validate_input_rejects_non_dict_directly():
    # The handler coerces non-dict args to {}, but the guard is defensive.
    with pytest.raises(ValueError):
        validation.validate_input("not a dict")


def test_coerce_normalizes_types():
    r = domain.ImprovedBugReport.from_parsed(EXAMPLE_A)
    assert isinstance(r.reproduction_steps, list)
    assert all(isinstance(s, str) for s in r.missing_evidence)


def test_coerce_truncates_overlong_title():
    r = domain.ImprovedBugReport.from_parsed(dict(EXAMPLE_B, title="T" * 200))
    assert len(r.title) <= schema.MAX_TITLE_CHARS
    assert r.title.endswith("…")


def test_coerce_flattens_title_newlines():
    r = domain.ImprovedBugReport.from_parsed(dict(EXAMPLE_B, title="line one\nline two"))
    assert r.title == "line one line two"


def test_title_invariant_holds_on_direct_construction():
    # __post_init__ enforces the single-line, length-capped title for any
    # construction path, not only via from_parsed.
    r = domain.ImprovedBugReport(
        title="x" * 200,
        summary="",
        reproduction_steps=[],
        expected_behavior="",
        actual_behavior="",
        severity="unknown",
        severity_rationale="",
        missing_evidence=[],
    )
    assert len(r.title) <= schema.MAX_TITLE_CHARS and r.title.endswith("…")


# --- prompt / rubric consistency ----------------------------------------------
def test_few_shot_examples_conform_to_schema():
    for ex in prompts.FEW_SHOT_EXAMPLES:
        r = domain.ImprovedBugReport.from_parsed(ex["output"])
        assert len(r.title) <= 80


def test_rubric_keys_match_severity_levels():
    assert tuple(schema.SEVERITY_RUBRIC) == schema.SEVERITY_LEVELS


def test_instructions_include_rubric_and_examples():
    ins = prompts.build_instructions()
    assert ins.count("Input:") == len(prompts.FEW_SHOT_EXAMPLES)
    for level in schema.SEVERITY_LEVELS:
        assert f"- {level}:" in ins


# --- registration (covers __init__.register) -----------------------------------
def test_register_wires_tool():
    import hermes_bug_report_improver as plugin

    captured: dict = {}

    class RegCtx:
        llm = None

        def register_tool(self, **kw):
            captured.update(kw)

    plugin.register(RegCtx())
    assert captured["name"] == "improve_bug_report"
    assert captured["toolset"] == "qa"
    assert captured["schema"]["name"] == "improve_bug_report"
    assert "raw_text" in captured["schema"]["parameters"]["properties"]
    assert callable(captured["handler"])


# --- optional /improve-bug slash command ---------------------------------------
def test_command_usage_on_empty(mock_ctx):
    cmd = handler.make_command(mock_ctx())
    assert cmd("").startswith("Usage:")
    assert cmd("   ").startswith("Usage:")


def test_command_returns_markdown_on_success(mock_ctx):
    out = handler.make_command(mock_ctx([EXAMPLE_B]))("checkout broken on safari")
    assert out.startswith("# ") and "## Severity: high" in out


def test_command_prettifies_error(no_llm_ctx):
    out = handler.make_command(no_llm_ctx)("something")
    assert out.startswith("Could not improve the report:")


def test_register_also_registers_command():
    import hermes_bug_report_improver as plugin

    tools: dict = {}
    cmds: dict = {}

    class C:
        llm = None

        def register_tool(self, **kw):
            tools.update(kw)

        def register_command(self, **kw):
            cmds.update(kw)

    plugin.register(C())
    assert tools["name"] == "improve_bug_report"
    assert cmds["name"] == "improve-bug" and callable(cmds["handler"])


def test_register_survives_command_failure():
    import hermes_bug_report_improver as plugin

    tools: dict = {}

    class C:
        llm = None

        def register_tool(self, **kw):
            tools.update(kw)

        def register_command(self, **kw):
            raise TypeError("unexpected kwarg args_hint")

    plugin.register(C())  # must not raise
    assert tools["name"] == "improve_bug_report"  # core tool still registered


# --- Hermes compatibility shim (root __init__.py) ------------------------------
# Hermes loads a plugin by executing the root __init__.py via importlib with
# submodule_search_locations, NOT by putting the (hyphenated) plugin directory on
# sys.path. This reproduces that load from a foreign CWD with PYTHONPATH cleared
# and the package not installed, so it fails if the shim stops making
# `hermes_bug_report_improver` importable on its own.
# Unified-plugin adaptation: the root __init__.py now registers the whole
# unified surface (positionally for some stages), so the probe context accepts
# any signature and the assertion is membership of this stage's tool.
_HERMES_LOADER = """\
import importlib.util, sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "flaky_stabilization_plugin_bugreport_probe",
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

captured = {}
class Ctx:
    llm = None
    def register_tool(self, *a, **kw):
        name = kw.get("name", a[0] if a else None)
        captured[name] = kw.get("toolset", a[1] if len(a) > 1 else None)
    def register_command(self, *a, **kw):
        pass
    def register_cli_command(self, *a, **kw):
        pass
    def register_hook(self, *a, **kw):
        pass
    def register_skill(self, *a, **kw):
        pass

mod.register(Ctx())
assert captured.get("improve_bug_report") == "qa", captured
print("SHIM_OK")
"""


def test_root_shim_loads_the_way_hermes_does(tmp_path):
    root = Path(__file__).resolve().parent.parent.parent
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # don't let an inherited path import the package for us
    proc = subprocess.run(
        [sys.executable, "-c", _HERMES_LOADER, str(root)],
        cwd=str(tmp_path),  # a CWD that is NOT the plugin dir
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SHIM_OK" in proc.stdout
