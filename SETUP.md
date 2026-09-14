# ApplyPilot — Setup From Scratch

For someone setting up ApplyPilot on a fresh machine. Takes about 15 minutes.

**You do not need Skyvern** — it has been removed from this project entirely.
See [What you don't need](#what-you-dont-need) at the bottom.

---

## 1. Prerequisites

| Need | Why | Get it |
|---|---|---|
| Python 3.11+ | Everything | [python.org](https://www.python.org/downloads/) |
| A Gemini API key | Scoring, tailoring, cover letters | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free |
| An OpenRouter API key | The model that fills out forms | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Goose CLI | The engine that drives the browser | [block.github.io/goose](https://block.github.io/goose/docs/getting-started/installation/) |
| Google Chrome | Auto-apply drives a real browser | [google.com/chrome](https://www.google.com/chrome/) |
| Node.js 18+ | Runs the Playwright MCP server via `npx` | [nodejs.org](https://nodejs.org/) |
| Claude Code CLI | *Optional.* Fallback engine for jobs Goose can't finish | [claude.ai/code](https://claude.ai/code) |

The last five are only needed for `applypilot apply`. You can discover, score,
and tailor with just Python + a Gemini key.

**Why two different AI keys?** They do different jobs. Gemini reads job
descriptions and writes text (cheap, huge free tier). OpenRouter runs the agent
that actually clicks through application forms — about $0.05 per application.

---

## 2. Install

```bash
git clone https://github.com/Pickle-Pixel/ApplyPilot.git
cd ApplyPilot

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .

# Job-board scraping. Installed separately because python-jobspy pins an exact
# numpy version in its metadata that breaks pip's resolver, but works fine at
# runtime with any modern numpy.
pip install --no-deps python-jobspy
pip install pydantic tls-client requests markdownify regex

# The browser-driving engine. Installs to ~/.local/bin, so make sure that's
# on your PATH afterwards.
curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash
```

Check it landed:

```bash
goose --version
```

---

## 3. Run the setup wizard

```bash
applypilot init
```

This walks you through your resume, contact details, work authorization, target
roles, and API key — then writes everything to `~/.applypilot/`:

```
~/.applypilot/
├── .env             # your API keys
├── profile.json     # contact info, work authorization, salary expectations
├── resume.txt       # plain text — this is what the AI reads
├── resume.pdf       # what actually gets uploaded to applications
├── searches.yaml    # what job titles/locations to search
└── applypilot.db    # every job it finds, scores, and applies to
```

**Have both `resume.txt` and `resume.pdf` ready before you start.** The AI stages
read the text version; the apply stage uploads the PDF. If you only give it a
PDF, the AI stages won't run.

### Configuring the key by hand instead

The wizard writes the `.env` for you. If you'd rather do it yourself, or need to
change providers later:

```bash
mkdir -p ~/.applypilot
cp .env.example ~/.applypilot/.env
# then edit ~/.applypilot/.env
```

`.env.example` documents every variable ApplyPilot reads. The only required one
is an LLM provider key.

> ApplyPilot reads `~/.applypilot/.env`. A `.env` in the repo root is also read
> as a fallback and the `~/.applypilot` one wins on conflict — so don't keep
> both, or you'll spend an afternoon wondering which key is live.

---

## 4. Verify

```bash
applypilot doctor
```

Every line should read `OK`, except optional ones. It reports a **tier**:

- **Tier 1 — Discovery.** Python + pip. Finds and lists jobs.
- **Tier 2 — AI Scoring & Tailoring.** + an LLM key. Scores jobs against your resume, tailors, writes cover letters.
- **Tier 3 — Full Auto-Apply.** + Chrome, Node.js, and an apply backend
  (Goose + an OpenRouter key, or the Claude Code CLI). Actually submits.

If you're stuck below the tier you want, `doctor` names the exact missing piece.

---

## 5. First run

```bash
# Discover → enrich → score. Start here; it makes no applications.
applypilot run discover enrich score

# See what it found
applypilot status
applypilot dashboard

# Fill out forms without submitting — always do this first
applypilot apply --dry-run --limit 1

# For real
applypilot apply --limit 5
```

Watch the first few. A Chrome window opens and drives itself; you'll want to see
what it does before turning it loose with `--workers 3` or `--continuous`.

### Optional: Gmail for verification codes

Many ATS platforms email a code or magic link mid-application. Authorize once:

```bash
npx -y @gongrzhe/server-gmail-autoauth-mcp auth
```

Credentials land in `~/.gmail-mcp/`. Without this, applications that hit an email
verification step stall out.

### Optional: the web UI

```bash
applypilot serve
```

Opens a browser tab at `http://127.0.0.1:8420` for browsing discovered jobs day
by day, filtering/sorting each day's table, and ticking the ones you want
applied to — instead of letting the ranked queue auto-pick. It only queues and
launches `applypilot apply` for you; the CLI commands above still work exactly
the same with `serve` never started. Loopback only, no `--host` flag, by
design (see [What you don't need](#what-you-dont-need)).

---

## How applying works

Two engines can drive the browser. Both use the exact same prompt, the same
Playwright + Gmail MCP servers, and the same Chrome:

| Backend | Model | Cost | Role |
|---|---|---|---|
| `goose` | OpenRouter (`xiaomi/mimo-v2.5`) | ~$0.05/application | **Default, and effectively the only engine in normal use.** Runs every job first. |
| `claude` | Claude Code CLI | Your Claude subscription quota | Rare fallback only. Retries only the jobs Goose couldn't finish. |

The fallback is deliberately narrow. Claude gets a second attempt only when
Goose gave up for a reason that means *the driver* lost the thread — it got
stuck, timed out, hit a broken page, or never reported an outcome. A posting
that's expired, already applied to, or behind an SSO wall is just as dead for
the stronger model, so those are never retried. In practice Goose on
`xiaomi/mimo-v2.5` finishes the large majority of jobs itself, so Claude usage
should stay occasional — if it isn't, that's a sign something regressed on
the Goose side, not that Claude should become the primary engine.

Change any of it in `~/.applypilot/settings.json`:

```jsonc
{
  "apply_backend": "goose",           // or "claude"
  "apply_fallback_backend": "claude", // or null to disable the retry
  "goose_model": "xiaomi/mimo-v2.5",  // any OpenRouter model
  "goose_writes_quirks": true
}
```

Or per run:

```bash
applypilot apply --backend claude       # skip Goose entirely
applypilot apply --fallback none        # Goose only, no second attempt
```

### The known-quirks cache

When a run discovers that some ATS widget needs a non-obvious interaction, it
records a `QUIRK:` note against that platform. Every later application to the
same ATS gets those notes in its prompt, on either backend — so the fleet
gets better at Workday, Greenhouse, and friends over time.

`goose_writes_quirks` controls whether Goose may *add* to that cache; reading
it is always on. It ships enabled and should stay that way — `xiaomi/mimo-v2.5`
is the trusted, always-on quirk writer now that it's the only engine in
regular use. Only turn it off if junk entries start showing up.

## What you don't need

**Skyvern.** Removed from the project. It was an alternative apply backend
that required running a separate Skyvern server; Goose does the same job with
no server to run. If you have a `~/skyvern` directory or a `~/.venvs/skyvern`
from an older setup, nothing here uses them and you can delete them. Any
`SKYVERN_*` lines in your `.env` are ignored.

Note that `applypilot serve` does start a local web server, which is not a
walking back of the above. Skyvern was a service the *apply pipeline* depended
on to function; `serve` is an optional UI for you, bound to `127.0.0.1`, that
the pipeline neither knows nor cares about. The CLI works exactly as it did
with the server never started.

**Someone else's `.env`.** Don't copy it, and don't let anyone copy yours:

- Gemini's free tier is quota-limited **per key**. Two people on one key means
  two people fighting over one daily allowance, and the pipeline dies mid-run
  when it's exhausted.
- OpenRouter keys are billed. A shared key means shared charges.
- Sharing API keys generally violates the provider's terms.
- A working `.env` usually carries personal overrides — a temporary `LLM_URL`
  pointing at some model someone was testing, for instance — which fail
  confusingly on a fresh setup.

Get your own keys. Gemini's is free and takes about a minute.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No LLM provider configured` | No `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `LLM_URL` in `~/.applypilot/.env` |
| AI stages skipped, no error | Only `resume.pdf` exists — the AI stages need `resume.txt` |
| Gemini 429s mid-run | Daily free quota hit. Set `OPENROUTER_API_KEY` for the automatic fallback (see `.env.example` §2) |
| Model isn't the one you set | An `LLM_URL` is set somewhere — it overrides `GEMINI_API_KEY` entirely |
| Apply hangs at a verification email | Gmail MCP not authorized (see step 5) |
| `Goose CLI MISSING` | Install it, then confirm `~/.local/bin` is on your PATH |
| `OpenRouter key MISSING` | Set `OPENROUTER_API_KEY` in `~/.applypilot/.env` |
| Every job falls back to Claude | Goose isn't finishing forms — check `~/.applypilot/logs/worker-*.log`, and try a stronger `goose_model` |
| Want to stop using Claude quota entirely | `applypilot apply --fallback none` |
