"""Goose backend: command construction, stream-json parsing, failure taxonomy.

The parsing tests use envelopes captured from a real `goose run
--output-format stream-json` session, since that shape is the contract this
backend depends on and nothing else in the repo pins it.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from applypilot import config
from applypilot.apply import outcomes
from applypilot.apply.backends import BACKEND_NAMES, get_backend
from applypilot.apply.backends.goose import (
    _build_command, _describe_tool, _extension_args, _strip_extension_prefix,
    run_job,
)


# ---------------------------------------------------------------------------
# Defaults / registry
# ---------------------------------------------------------------------------

def test_goose_is_the_default_backend():
    assert config.DEFAULT_SETTINGS["apply_backend"] == "goose"


def test_goose_is_listed_first_and_skyvern_is_gone():
    assert BACKEND_NAMES == ("goose", "claude")


def test_unnamed_backend_resolves_to_goose():
    assert get_backend("").name == "goose"


def test_both_backends_construct():
    assert get_backend("goose").name == "goose"
    assert get_backend("claude").name == "claude"


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        get_backend("skyvern")


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

def test_command_pins_provider_model_and_stream_format():
    cmd = _build_command(9222, "xiaomi/mimo-v2.5", "openrouter")
    assert cmd[:2] == ["goose", "run"]
    for flag, value in [("--provider", "openrouter"),
                        ("--model", "xiaomi/mimo-v2.5"),
                        ("--output-format", "stream-json")]:
        assert cmd[cmd.index(flag) + 1] == value


def test_command_bounds_the_run():
    """A cheap model that loses the thread must not loop forever."""
    cmd = _build_command(9222, "m", "openrouter")
    assert "--max-turns" in cmd
    assert "--max-tool-repetitions" in cmd
    assert "--no-session" in cmd  # every job starts clean


def test_extension_args_point_playwright_at_this_workers_port():
    """Parallel workers each have their own Chrome; the CDP port is what
    keeps one worker's agent out of another worker's browser."""
    args = _extension_args(9227)
    specs = [a for a in args if not a.startswith("--")]
    playwright = next(s for s in specs if s.startswith("playwright:"))
    assert "--cdp-endpoint=http://localhost:9227" in playwright
    assert any(s.startswith("gmail:") for s in specs)


def test_each_extension_spec_is_a_single_argv_entry():
    """Goose parses '[name:]command args...' itself -- splitting the spec on
    spaces would make it read the flags as separate extensions."""
    args = _extension_args(9222)
    assert args.count("--with-extension") == 3  # playwright, gmail, applytools
    assert len(args) == 6


# ---------------------------------------------------------------------------
# Tool-name handling
# ---------------------------------------------------------------------------

def test_strip_extension_prefix():
    assert _strip_extension_prefix("playwright__browser_navigate") == "browser_navigate"
    assert _strip_extension_prefix("gmail__search_emails") == "search_emails"
    assert _strip_extension_prefix("browser_navigate") == "browser_navigate"


def test_navigation_detection_survives_the_goose_prefix():
    """Regression: Goose namespaces tools as `playwright__browser_navigate`,
    so matching the bare name against the raw tool name never fired and the
    ATS was never resolved from the URLs the agent actually visited."""
    assert _strip_extension_prefix("playwright__browser_navigate") == "browser_navigate"


@pytest.mark.parametrize("name,args,ext,expected", [
    ("playwright__browser_navigate", {"url": "https://x.com/j/1"}, "playwright",
     "browser_navigate https://x.com/j/1"),
    ("gmail__search_emails", {"query": "code"}, "gmail", "gmail:search_emails"),
    ("playwright__browser_file_upload", {"paths": ["/r.pdf"]}, "playwright",
     "browser_file_upload upload"),
    ("playwright__browser_fill_form", {"fields": [1, 2, 3]}, "playwright",
     "browser_fill_form (3 fields)"),
])
def test_describe_tool(name, args, ext, expected):
    assert _describe_tool(name, args, ext) == expected


# ---------------------------------------------------------------------------
# stream-json parsing
# ---------------------------------------------------------------------------

def _msg(*blocks):
    return json.dumps({"type": "message", "message": {"role": "assistant",
                                                      "content": list(blocks)}})


def test_text_is_streamed_one_token_per_envelope():
    """Goose emits each token as its own envelope, so a RESULT: line only
    exists once every text block is concatenated -- scanning them one at a
    time would never match."""
    stream = [
        _msg({"type": "thinking", "thinking": "hmm"}),
        _msg({"type": "text", "text": "RESULT"}),
        _msg({"type": "text", "text": ":AP"}),
        _msg({"type": "text", "text": "PLIED"}),
    ]
    text = []
    for line in stream:
        msg = json.loads(line)
        for b in msg["message"]["content"]:
            if b["type"] == "text":
                text.append(b["text"])
    assert "RESULT:APPLIED" in "".join(text)
    # ...and would not have matched per-block.
    assert not any("RESULT:APPLIED" in t for t in text)


def test_complete_envelope_carries_token_accounting():
    line = json.dumps({
        "type": "complete", "total_tokens": 31614, "input_tokens": 31396,
        "output_tokens": 218, "cache_read_input_tokens": 20672,
        "cache_write_input_tokens": 0, "cost_usd": 0.0016202816,
    })
    msg = json.loads(line)
    assert msg["type"] == "complete"
    assert msg["input_tokens"] == 31396
    assert msg["cache_read_input_tokens"] == 20672
    assert msg["cost_usd"] > 0


def test_tool_request_shape():
    block = {
        "type": "toolRequest",
        "id": "call_1",
        "toolCall": {"status": "success", "value": {
            "name": "playwright__browser_navigate",
            "arguments": {"url": "https://boards.greenhouse.io/acme/jobs/1"},
        }},
        "_meta": {"goose_extension": "playwright"},
    }
    call = block["toolCall"]["value"]
    assert _strip_extension_prefix(call["name"]) == "browser_navigate"
    assert call["arguments"]["url"].startswith("https://")
    assert block["_meta"]["goose_extension"] == "playwright"


# ---------------------------------------------------------------------------
# Failure categorization (failure_taxonomy.py's dashboard grouping)
# ---------------------------------------------------------------------------

def test_driver_gave_up_reasons_are_not_also_permanent_failures():
    """The two sets must not overlap, or the failure-reasons dashboard would
    double-count a permanent failure under the "driver gave up" bucket too."""
    overlap = outcomes.FALLBACK_REASONS & outcomes.PERMANENT_FAILURES
    assert overlap == set()


# ---------------------------------------------------------------------------
# CDP preflight
# ---------------------------------------------------------------------------

def test_run_job_fails_fast_when_cdp_port_is_dead():
    """A dead DevTools port must fail before any LLM call, not after --
    on 9/14, 155 traces burned real spend against a port that was never up."""
    job = {"title": "x", "url": "http://example.invalid", "site": "x"}
    # Port 1 requires root and nothing listens there in CI/dev -- connection
    # refused immediately, same shape as a genuinely dead Chrome.
    status, duration_ms = run_job(job, port=1, worker_id=9999)
    assert status == "failed:cdp_unreachable"
    assert duration_ms == 0
