#!/bin/bash
# Ad-hoc cheap-model test: paste any job URL and an OpenRouter model, and this
# builds a real ApplyPilot prompt (untailored -- uses the default resume, not
# a job-specific one, since this URL was never scored/tailored through the
# normal pipeline) and runs it through Goose against the same Playwright MCP
# server the Claude Code backend uses. Always a dry run.
#
# Usage: scripts/goose_quicktest.sh <job_url> <openrouter_model> [test_index] [lane]
# Example: scripts/goose_quicktest.sh \
#   "https://company.wd5.myworkdayjobs.com/en-US/careers/job/12345" \
#   "deepseek/deepseek-v4-flash-0731" \
#   1 0
#
# The optional 3rd arg (an integer, 1-8) lets you re-run against the SAME
# employer's form for a real regression comparison, without the ATS
# remembering a prior run's account -- a repeat run under the real email
# showed up with an already-filled Application Questions page, which quietly
# erased most of the turn count that run was supposed to measure.
#
# Each test index maps to a distinct-looking but still real, deliverable
# address: Gmail ignores dots in the local part, so test index N inserts a
# dot at position N into the profile's own Gmail address -- for
# "someone@gmail.com", index 1 gives "s.omeone@gmail.com". All of them land
# in that same real inbox (the one the gmail MCP extension is authenticated
# against), so account recovery and email verification keep working. This
# needs the profile email to BE the Gmail inbox: a forwarding alias at
# another domain almost certainly matches the literal address only, not a
# dotted variant, so the script refuses a non-Gmail profile. The account
# password is also swapped to a fixed test password so these test signups
# never collide with or need the real STD_PASSWORD.
#
# Give each run its own index -- reusing one still pollutes state for the
# NEXT run at that index, though it's harmless for a true from-scratch retry.
#
# The optional 4th arg (a lane number, default 0) is what makes two of these
# safe to run AT THE SAME TIME. Each lane gets its own Chrome CDP port
# (9222 + lane), its own Chrome profile directory (launch_chrome's existing
# per-worker isolation), and its own prompt file -- without it, two
# concurrent runs would fight over the same port and profile, and the
# cleanup at the end of one would kill the Chrome instance the other is
# still using. Pass a different lane per concurrent run: lane 0 for one
# model, lane 1 for another, etc.
#
# Launching several lanes at once? Stagger the launches by a random 10-20s
# each, not a fixed short delay -- an 8-lane benchmark launched 2s apart
# (2026-09-18) fired 8 first-turn LLM calls close enough together that 3 hit
# "Provider timed out" against the same cheap OpenRouter model. Same reason
# production's worker_loop jitters every job start now, not just at boot.

set -e

JOB_URL="$1"
MODEL="$2"
TEST_INDEX="$3"
LANE="${4:-0}"
HEADLESS="${5:-false}"
PORT=$((9222 + LANE))

if [ -z "$JOB_URL" ] || [ -z "$MODEL" ]; then
  echo "Usage: $0 <job_url> <openrouter_model> [test_index 1-8] [lane, default 0] [headless true|false, default false]"
  echo "Example models: deepseek/deepseek-v4-flash-0731, z-ai/glm-5.3-flash, minimax/minimax-m3:free, xiaomi/mimo-v2.5, deepseek/deepseek-v4-flash-vision-exp"
  echo "Run two at once by giving them different lanes, e.g. ... 1 0   and   ... 2 1"
  exit 1
fi

# 2026-09-17: a dry-run batch against these two ignored the "do not click
# Submit" instruction and submitted for real -- SpaceX (Greenhouse) got a
# genuine duplicate application, Redwire Space (Paycor) got a real submit
# attempt that its own duplicate-detection blocked. "dry run" is a prompt
# instruction the model can and did disregard, not an enforced guarantee --
# refuse these companies outright rather than trust dry_run again.
for blocked in "greenhouse.io/embed/job_app?for=spacex" "careers.rdw.com"; do
  case "$JOB_URL" in
    *"$blocked"*)
      echo "REFUSING: $JOB_URL matches a company blocked after a dry-run instruction was ignored and produced a real submission. See this file's history for details."
      exit 1
      ;;
  esac
done

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="/tmp/goose_quicktest_prompt_${LANE}.txt"

# Load OPENROUTER_API_KEY *and* LANGFUSE_* from ApplyPilot's real .env.
# Only OPENROUTER_API_KEY used to be exported here -- goose's own OTEL
# exporter needs LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST in ITS process
# environment to send traces at all, and since this script never exported
# them, every goose_quicktest.sh run traced nowhere (confirmed 2026-09-17:
# zero traces in Langfuse across two full benchmark batches). Production
# runs never hit this because the parent web server process already has the
# whole .env loaded into os.environ before it spawns goose, so goose.py's
# `env = os.environ.copy()` picks it up for free.
env_vars=$("$REPO_DIR/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot import config
config.load_env()
import os
key = os.environ.get('OPENROUTER_API_KEY', '')
if not key:
    sys.exit('OPENROUTER_API_KEY is empty in ~/.applypilot/.env -- add it first.')
for name in ('OPENROUTER_API_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_HOST'):
    val = os.environ.get(name, '')
    if val:
        print(f'{name}={val}')
")
set -a
eval "$env_vars"
set +a

# Build the prompt exactly the way the real pipeline does (same build_prompt()
# function the Claude Code backend uses -- same profile, same eligibility
# rules, same known-quirks cache for whatever ATS this URL resolves to), but
# with a synthetic job dict since this URL skipped discovery/scoring/tailoring.
"$REPO_DIR/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot import config
from applypilot.apply import prompt as prompt_mod

txt_path, pdf_path = config.get_resume_paths()
resume_text = txt_path.read_text(encoding='utf-8') if txt_path.exists() else ''

job = {
    'title': 'Manual test job (untailored resume)',
    'site': 'manual-test',
    'url': '$JOB_URL',
    'application_url': '$JOB_URL',
    'fit_score': 'N/A',
    'tailored_resume_path': str(pdf_path.with_suffix('')),
    'resume_variant': 'default',
}

test_index = '$TEST_INDEX'
email_override = None
password_override = None
if test_index:
    # Derive the alias from whatever address this machine's profile uses --
    # hardcoding one leaked a real address into a shared repo, and the trick
    # only works against the inbox the gmail extension is authed against
    # anyway, which is the profile's own.
    real_email = config.load_profile().get('personal', {}).get('email', '')
    if '@' not in real_email:
        sys.exit('profile.json has no personal.email -- cannot build a test alias.')
    local_part, domain = real_email.split('@', 1)
    # Gmail-style dot-insensitivity also holds on any Google Workspace-hosted
    # domain (e.g. a .edu that routes mail through Google) -- confirmed
    # working for account creation AND password reset on this profile's
    # domain, so this is a soft warning rather than a hard gate.
    if domain.lower() not in ('gmail.com', 'googlemail.com'):
        print('NOTE: profile email is on ' + domain + ', not gmail.com -- the '
              'dot-insensitive alias trick only works if this domain is '
              'Google-hosted mail. Proceeding on the assumption it is.')
    n = int(test_index)
    n = max(1, min(n, len(local_part) - 1))
    dotted = local_part[:n] + '.' + local_part[n:]
    email_override = dotted + '@' + domain
    password_override = 'JanuszO1234!'
    print('Test account:', email_override)

p = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, dry_run=True,
                             email_override=email_override,
                             password_override=password_override)
open('$PROMPT_FILE', 'w').write(p)
print('Prompt written to $PROMPT_FILE (', len(p), 'chars)')
"

"$REPO_DIR/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot import config
config.load_env()
from applypilot.apply import chrome
p = chrome.launch_chrome(worker_id=$LANE, port=$PORT, headless=$([ "$HEADLESS" = "true" ] && echo True || echo False))
print('chrome up on lane $LANE, port $PORT, pid', p.pid)
" 2>&1 | grep -v NumExpr

# launch_chrome logs a startup failure but still returns the (dead) process
# handle rather than raising -- confirmed the actual cause here: without the
# config.load_env() above, CHROME_PATH from .env was never read, so
# get_chrome_path() fell back to the sandboxed system chromium-browser
# instead of the Playwright-managed one, which can't create its
# SingletonLock in a worker profile dir it doesn't own the AppArmor
# confinement for. Confirm this launch actually came up before running
# goose against it, instead of burning a full turn budget against a dead
# port and only finding out from a Playwright ECONNREFUSED loop.
for i in $(seq 1 20); do
  curl -s -m 1 "http://localhost:$PORT/json/version" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -s -m 2 "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
  echo "ERROR: Chrome never came up on port $PORT -- aborting before spending any LLM calls."
  exit 1
fi

# Free network-usage telemetry (see network_stats.py's docstring): the
# applytools extension below polls performance.getEntriesByType() on this
# same CDP connection and writes real transferred bytes here. Reset first
# so a stale file from an earlier run on this same port/lane isn't
# mistaken for this run's numbers.
NETSTATS_FILE="/tmp/applypilot_netstats_${PORT}.json"
rm -f "$NETSTATS_FILE"

START=$(date +%s)
# applytools -- the same third extension the real pipeline wires up (see
# goose.py's _extension_args). Missing here for a while, which meant this
# script could never actually exercise human_fill_form/read_form_state/
# check_for_errors/etc. -- only the raw browser_* tools.
#
# --no-profile was ALSO missing -- without it goose loads the account's
# default profile extensions on top of the ones passed here, which on this
# VM includes a shell/developer toolkit the real backend never grants (see
# goose.py's _build_command, which always passes --no-profile). Confirmed
# live: a stuck run used that shell access to `cat ~/.applypilot/.env` and
# dump every API key/password in it straight into the run's own log and
# into the calling agent's transcript. Real production runs can't do this
# (no shell extension at all); this ad-hoc script must match that, not
# grant more than production ever does.
# stream-json, not the human-readable renderer -- `--stats` in text mode only
# prints the last turn's tokens/sec, never a cost figure. cost_usd only ever
# appears in stream-json's final "complete" event (see goose.py's own
# parser, which this filter deliberately mirrors), and Langfuse's totalCost
# reads 0 here for every trace checked, including real production runs --
# confirmed 2026-09-17 after a benchmark that captured no cost data at all.
cat "$PROMPT_FILE" | goose run --no-session --no-profile -i - \
  --provider openrouter --model "$MODEL" \
  --output-format stream-json \
  --with-extension "playwright:npx @playwright/mcp@latest --cdp-endpoint=http://localhost:$PORT --viewport-size=1280x900" \
  --with-extension "gmail:npx -y @gongrzhe/server-gmail-autoauth-mcp" \
  --with-extension "applytools:$REPO_DIR/.venv/bin/python3 -m applypilot.apply.mcp_tools.server --cdp-endpoint=http://localhost:$PORT --dry-run" \
  | "$REPO_DIR/.venv/bin/python3" -u -c "
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        print(line)  # goose's startup ASCII banner, not JSON
        continue

    t = msg.get('type')
    if t == 'complete':
        print()
        print('COST_USD=%s INPUT_TOKENS=%s OUTPUT_TOKENS=%s CACHE_READ_TOKENS=%s' % (
            msg.get('cost_usd'), msg.get('input_tokens'),
            msg.get('output_tokens'), msg.get('cache_read_input_tokens')))
        continue
    if t != 'message':
        continue

    for block in msg.get('message', {}).get('content', []) or []:
        bt = block.get('type')
        if bt == 'text':
            sys.stdout.write(block.get('text', ''))
        elif bt == 'toolRequest':
            call = (block.get('toolCall') or {}).get('value') or {}
            name = call.get('name', 'unknown')
            print()
            print('  ▸', name)
        elif bt == 'toolResponse':
            print()
            print('  ◂', json.dumps(block)[:2000])
sys.stdout.flush()
"
echo "GOOSE_EXIT=${PIPESTATUS[0]}"
echo "ELAPSED_SECONDS=$(( $(date +%s) - START ))"

if [ -f "$NETSTATS_FILE" ]; then
  echo "NETWORK_STATS=$(cat "$NETSTATS_FILE")"
else
  echo "NETWORK_STATS=unavailable (applytools extension was never called this run)"
fi

pkill -f "remote-debugging-port=$PORT" 2>/dev/null
echo "chrome cleaned up (lane $LANE, port $PORT)"
