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

set -e

JOB_URL="$1"
MODEL="$2"
TEST_INDEX="$3"
LANE="${4:-0}"
PORT=$((9222 + LANE))

if [ -z "$JOB_URL" ] || [ -z "$MODEL" ]; then
  echo "Usage: $0 <job_url> <openrouter_model> [test_index 1-8] [lane, default 0]"
  echo "Example models: deepseek/deepseek-v4-flash-0731, z-ai/glm-5.3-flash, minimax/minimax-m3:free, xiaomi/mimo-v2.5, deepseek/deepseek-v4-flash-vision-exp"
  echo "Run two at once by giving them different lanes, e.g. ... 1 0   and   ... 2 1"
  exit 1
fi

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="/tmp/goose_quicktest_prompt_${LANE}.txt"

# Load OPENROUTER_API_KEY from ApplyPilot's real .env, not a stray /tmp file.
export OPENROUTER_API_KEY=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot import config
config.load_env()
import os
key = os.environ.get('OPENROUTER_API_KEY', '')
if not key:
    sys.exit('OPENROUTER_API_KEY is empty in ~/.applypilot/.env -- add it first.')
print(key)
")

# Build the prompt exactly the way the real pipeline does (same build_prompt()
# function the Claude Code backend uses -- same profile, same eligibility
# rules, same known-quirks cache for whatever ATS this URL resolves to), but
# with a synthetic job dict since this URL skipped discovery/scoring/tailoring.
python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot import config
from applypilot.apply import prompt as prompt_mod

txt_path, pdf_path, grad_date = config.get_resume_variant_paths('default')
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
    if domain.lower() not in ('gmail.com', 'googlemail.com'):
        sys.exit('The dotted-alias trick is Gmail-only; profile email is ' + domain + '.')
    n = int(test_index)
    n = max(1, min(n, len(local_part) - 1))
    dotted = local_part[:n] + '.' + local_part[n:]
    email_override = dotted + '@' + domain
    password_override = 'Password123!'
    print('Test account:', email_override)

p = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, dry_run=True,
                             email_override=email_override,
                             password_override=password_override)
open('$PROMPT_FILE', 'w').write(p)
print('Prompt written to $PROMPT_FILE (', len(p), 'chars)')
"

python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR/src')
from applypilot.apply import chrome
p = chrome.launch_chrome(worker_id=$LANE, port=$PORT, headless=False)
print('chrome up on lane $LANE, port $PORT, pid', p.pid)
" 2>&1 | grep -v NumExpr

START=$(date +%s)
cat "$PROMPT_FILE" | goose run --no-session -i - \
  --provider openrouter --model "$MODEL" \
  --with-extension "playwright:npx @playwright/mcp@latest --cdp-endpoint=http://localhost:$PORT --viewport-size=1280x900" \
  --with-extension "gmail:npx -y @gongrzhe/server-gmail-autoauth-mcp"
echo "GOOSE_EXIT=$?"
echo "ELAPSED_SECONDS=$(( $(date +%s) - START ))"

pkill -f "remote-debugging-port=$PORT" 2>/dev/null
echo "chrome cleaned up (lane $LANE, port $PORT)"
