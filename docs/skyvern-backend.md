# Skyvern apply backend

`applypilot apply` can drive the browser two ways:

| Backend | Engine | Cost |
|---|---|---|
| `claude` (default) | `claude -p` + Playwright MCP | Claude subscription quota |
| `skyvern` | Local Skyvern server over CDP | Whatever model Skyvern is configured with |

The point of the Skyvern backend is to run applications on a cheap or free
OpenRouter model so the Claude Pro 5-hour window stays free for interactive
work. Both backends apply as the same candidate under the same rules — profile,
eligibility, salary strategy and screening guidance are shared code
(`apply/prompt.py:_prepare_context`) — and both write the same reason codes to
the database, so their completion rates are directly comparable.

Skyvern attaches to the Chrome that ApplyPilot already launched for the worker,
so it inherits the cloned profile and its ATS session cookies. It does not open
its own browser.

## Setup

### 1. Install

```bash
pip install "applypilot[skyvern]"     # or: pip install skyvern
```

Requires Python 3.11–3.13 (Skyvern does not yet support 3.14).

### 2. Configure Skyvern's own `.env`

The **model lives in Skyvern's config, not ApplyPilot's.** Minimum viable setup:

```bash
ENABLE_OPENROUTER=true
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=minimax/minimax-m3:free
LLM_KEY=OPENROUTER
LLM_CONFIG_SUPPORT_VISION=true
ALLOWED_HOSTS=["127.0.0.1"]
```

Two of these are easy to get wrong and both fail quietly:

- **`ALLOWED_HOSTS=["127.0.0.1"]` is required.** ApplyPilot serves the tailored
  resume over loopback (Skyvern uploads files by downloading them first, and
  local paths are only accepted inside Skyvern's own per-run directory). Skyvern
  ships with `BLOCKED_HOSTS = ["localhost"]` and an SSRF guard that rejects
  private IPs; an entry in `ALLOWED_HOSTS` bypasses it. Without this the resume
  never uploads and the application submits incomplete.
- **`LLM_CONFIG_SUPPORT_VISION` must match the model.** Set it `true` only for a
  model that actually accepts images. With it `false`, Skyvern runs DOM-only —
  which is the same accessibility-tree approach that struggles with custom
  dropdowns in the first place.

### 3. Configure ApplyPilot's `~/.applypilot/.env`

```bash
SKYVERN_BASE_URL=http://localhost:8000
SKYVERN_API_KEY=...      # printed by `skyvern run server`, or in Skyvern's .env
```

### 4. Run the server

```bash
skyvern run server
```

ApplyPilot connects to it; it does not start or supervise it. `apply` refuses to
start with a clear message if the server is unreachable, the package is missing,
or the API key is unset — before Chrome launches or a job is locked.

## Usage

```bash
applypilot apply --backend skyvern --url <job-url> --dry-run   # single job, no submit
applypilot apply --backend skyvern --limit 5                   # real run
applypilot apply --backend claude                              # unchanged original path
```

Set a persistent default in `~/.applypilot/settings.json`:

```json
{ "apply_backend": "skyvern" }
```

`--model` is ignored by the Skyvern backend; change the model in Skyvern's `.env`.

## Comparing backends

Every application records which backend produced it:

```sql
SELECT apply_backend,
       COUNT(*)                                                  AS attempts,
       SUM(apply_status = 'applied')                             AS applied,
       ROUND(100.0 * SUM(apply_status = 'applied') / COUNT(*), 1) AS pct,
       ROUND(AVG(apply_duration_ms) / 1000.0, 1)                 AS avg_secs
FROM jobs
WHERE apply_backend IS NOT NULL
GROUP BY apply_backend;
```

Completion rate is the number that matters, not cost per run: a model that dies
mid-form burns its steps *and* doesn't apply to the job.

## Suggested model progression

Start free to shake out integration bugs at zero cost, then move up only if
completion rate disappoints:

| Model | ~$/application | Notes |
|---|---|---|
| `minimax/minimax-m3:free` | $0 | 20 req/min; 50/day under $10 lifetime spend, 1000/day after |
| `qwen/qwen3.7-flash` | ~$0.04 | Cheapest paid option worth trying |
| `google/gemini-2.5-flash` | ~$0.44 | Most reliable structured-JSON emitter in this band |

Estimates assume ~30–80 LLM calls per application; measure your own.

Note that free OpenRouter endpoints generally carry data-training permissions,
and the prompt contains the candidate's name, contact details, work
authorization and salary expectations. The paid endpoint of the same model
avoids that and removes the rate limits. (EEO/demographic fields are never sent
by either backend.)

## Email verification

Both code-based and link-based verification are handled automatically.

Gmail access reuses the OAuth token the Gmail MCP server already stored in
`~/.gmail-mcp/` — no second authorisation, and no extra Python dependency
(it refreshes the token and calls the REST API with `httpx`). The refreshed
access token is held in memory only, so it never races with the MCP server's
own `credentials.json`.

Searches use `in:anywhere`, which covers **spam** — forwarded ATS mail routinely
fails SPF/DKIM at the destination and gets filed there, and these codes usually
expire in ~10 minutes.

| Email contains | What happens |
|---|---|
| A code (`483920`) | Skyvern POSTs to the local `totp_url` endpoint; it polls Gmail up to 45s and answers `{"verification_code": "483920"}` |
| A link ("click to verify") | A background watcher opens it in a **background tab of the same Chrome** over CDP, then closes the tab. The auth cookie is now set browser-wide, so the page Skyvern is driving can continue |

The link path exists because Skyvern's `totp_url` contract only carries a code —
there is nowhere to *type* a URL. Skyvern's own documented answer is to split the
run into two tasks; opening the link in the same browser keeps it to one run.

Only mail that arrives **after the run starts** is considered, so a stale code
from an earlier application is never replayed.

Requires `ALLOWED_HOSTS=["127.0.0.1"]` in Skyvern's `.env` — the same setting the
resume upload needs, since Skyvern's SSRF guard also covers its outbound call to
`totp_url`.

If Gmail is not authorised, verification degrades quietly: the endpoint returns
404, and a login needing a code ends as `login_issue`. To (re)authorise:

```bash
npx -y @gongrzhe/server-gmail-autoauth-mcp auth
```

## Known gaps

- **No step-level dashboard progress.** Skyvern runs server-side, so the worker
  row shows "skyvern running" rather than a live action count. `run_id`,
  `recording_url` and the run's UI link are written to
  `~/.applypilot/logs/worker-N.log`.
- **Rate limits vs. parallel workers.** All workers share one OpenRouter
  account limit, so `--workers > 1` on a free model mostly produces 429s.
