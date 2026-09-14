# ApplyPilot Handoff

Living doc, rewritten each session rather than appended to. For the
historical record of what shipped, see `CHANGELOG.md`. This is "what do I
need to know to pick this up right now."

Last updated: 2026-09-03

## Current state

**Deployment:** the live pipeline runs continuously on an Oracle Cloud VM
(`ssh oracle-applypilot`), not locally. Check there for real run state, logs,
and the actual `applypilot.db` -- local checkouts are for development only.

**Apply agent (Goose on `xiaomi/mimo-v2.5` + Playwright MCP):** this is the
primary and effectively only engine in regular use now. Claude Code is kept
wired up as a narrow, rarely-firing fallback (see `apply_fallback_backend`)
for jobs where Goose itself lost the thread -- it is not a co-equal path and
should not be treated as one in cost/behavior reasoning. Real confirmed
applications on Workday, SAP SuccessFactors, and simple-form ATS platforms.
Cost/turn data is real, not estimated -- see the per-ATS cost table below.

**Discovery:** Intern List and NewGrad Jobs both extract at 100% (22 and 27
jobs respectively, verified via `applypilot run discover`). Both ultimately
source from Jobright's backend, just through different front-ends (Jobright
iframe vs. an Airtable grid embed).

**Enrichment / ATS resolution: DONE, verified at scale.** 120/121 jobs
(99%) resolved to a real employer URL with correct ATS classification, up
from 2/120 (1.7%) before this session's fixes. Real distribution across the
queue: Workday 41, iCIMS 12, SAP SuccessFactors 10, Greenhouse 7, Eightfold
6, Taleo 5, Oracle HCM 3, Dayforce 3, Ashby 3, BrassRing 1, ADP 1, 4 still
unresolved. The key fix was `resolve_original_job_url` clicking Jobright's
stable "Original Job Post" toolbar link (present on every job page
regardless of which Apply-button variant that posting shows) instead of
chasing Apply-button text variants ("Apply Now" vs "Apply With Autofill",
possibly more not yet seen) -- one click, no upsell dialog, works
universally. See "Known gotchas" below for the three real bugs found and
fixed getting here.

This now unblocks the `is_hard_to_automate` scoring nudge (see Open Items)
-- there's finally enough real `ats` data across the queue to act on.

**Cheap-model testing (Goose + OpenRouter):** MiMo-V2.5 is the current best
result -- $0.045, 140 turns, on a real Workday form. That single-job test
measured a ~99% cache-hit ratio, but the real fleet-wide average (from the
VM's `applypilot.db`, across 172 goose runs) is only ~50%, and barely moves
with run length (~47% on runs under 30 turns vs. ~50% on runs over 150) --
this is being actively investigated, see cost-saving investigation notes.
GLM 5.3 Flash and DeepSeek V4 Flash Vision Exp both landed around $0.33-0.37
due to per-model inefficiencies (GLM: reading Playwright's own snapshot
files via shell instead of `browser_find`, now guarded against in the
prompt; DeepSeek: heavy `browser_evaluate` usage, not yet addressed).

**Grad-date / resume-variant handling:** just solidified this session.
`settings.json` has two variants (`default` = May 2027 grad / August 2027
start, `returning_2028` = January 2028 grad / February 2028 start -- **the
returning_2028 resume file itself still says "May 2028"; the user is
replacing it, config is already updated to January 2028**). The apply prompt
now actively verifies the resume's graduation date against what the form's
dropdown/requirements actually allow, and outputs
`RESULT:FAILED:grad_date_mismatch` rather than pushing through a doomed
submission. On that specific failure, `launcher.swap_resume_variant_for_retry()`
automatically switches the job to the other variant so the retry (already
permitted -- this failure isn't in `PERMANENT_FAILURES`) uses a resume that
actually matches. Verified the swap function directly; **not yet verified
end-to-end on a real job that actually hits a real mismatch** -- no real
example encountered yet.

## Real cost data (not estimates)

| ATS platform | Real cost | Notes |
|---|---|---|
| Simple form (Redwire) | $1.71 | 57 turns, cleanest baseline |
| Workday (Copart) | $3.97 | 111 turns |
| Workday (Air Products) | $5.58 | 172 turns -- date-field fight before the fix |
| SAP SuccessFactors (Qorvo) | $9.45 | account recovery tripled the cost |

`ats.py`'s `is_hard_to_automate()` flags SAP SuccessFactors, Oracle HCM,
Taleo, iCIMS as the expensive tier -- confirmed by the above, not just
theory. Proposed but **not yet implemented**: fold this into queue-ordering
as a small priority nudge (same pattern as the age-decay), not a hard gate.

## Open items, roughly in priority order

1. **`jobright_resolve_pacing_seconds` (8s delay) is very likely dead
   weight now** -- it was added on a since-disproven rate-limiting theory;
   the real bug was the resolver never navigating to the candidate URL
   (fixed) and matching only one Apply-button text variant (also fixed).
   Now that resolution is 99% reliable without needing the delay to explain
   it, try removing the pacing and re-running a batch to confirm it's still
   99% -- if so, drop it, since it's currently adding ~8s x every
   aggregator-sourced job to every enrichment run for no benefit.
2. **is_hard_to_automate scoring nudge** -- now unblocked, real `ats` data
   exists for 120/121 jobs. Not yet implemented.
3. **Original-posting re-scrape during enrichment + skills-field keyword
   augmentation** -- both just added (see CHANGELOG), not yet run against a
   real batch. Verify the original-posting description actually supersedes
   Jobright's summary when richer, and that a real `--rescore` run populates
   the new `keywords` column before trusting the skills-field prompt section
   does anything (it's a no-op until `keywords` is populated).
4. **NewGrad Jobs' remaining unknowns**: extraction is 100% now, but the
   Escape-vs-close-button DOM quirk that caused the original every-other-row
   failure was fixed without fully understanding Airtable's internal
   virtualization -- if it breaks again, check `_scrape_airtable_button_grid`
   in `discovery/smartextract.py` first.
5. **Grad-date mismatch handling** -- built and unit-tested, never fired on
   a real mismatch. Worth deliberately testing (e.g. `--gen` a prompt for a
   job known to require a specific class year) before trusting it blind.
6. **Cover letters** are at 0 in the pipeline (`cover_letters_enabled:
   false` in settings.json) -- confirm this is intentional before assuming
   it's a bug.
7. **GLM/DeepSeek turn-efficiency** -- MiMo is currently the best cheap-model
   result; the other two have known, undiagnosed-further inefficiencies (see
   above). Not urgent given MiMo works.

## Known gotchas (things that will burn time if you don't know them)

- **`resolve_original_job_url` requires the page to already be navigated to
  the candidate URL.** It does NOT navigate there itself by default logic --
  it was patched to do so (`page.goto(candidate_url)` if not already there),
  but if you're calling it from a new code path, don't assume the page is
  positioned correctly. This exact assumption cost a very long debugging
  session (rate-limiting looked like the cause; it wasn't).
- **Jobright requires a signed-in session** for the real Apply flow to work
  at all -- logged out, it's a hard signup wall, not a dismissible popup.
  The session lives in `config.ENRICHMENT_PROFILE_DIR`
  (`~/.applypilot/chrome-enrichment-profile`), separate from the apply-
  workers' profiles on purpose (those get reset between runs). If ATS
  resolution suddenly stops working, check whether this session is still
  authenticated first (`text=SIGN IN` present on a job page means logged
  out).
- **Google SSO login does not work for automated browsers** -- Google
  blocks it as a security measure, unrelated to Jobright. Use Jobright's
  plain email+password sign-in instead (the modal has one, distinct from
  the Google/Apple buttons).
- **`applypilot run discover` re-runs both configured sites every time** --
  there's no per-site CLI filter, so testing one site's scraper in isolation
  means either calling the internal function directly (`_scrape_airtable_button_grid`,
  `_run_one_site`) or accepting the other site re-runs too (usually fast
  since it's mostly duplicate-skips).
- **Editable install**: `applypilot` the CLI command runs from this exact
  source tree (`pip show applypilot` confirms "Editable project location").
  A change here takes effect on the next CLI invocation, no reinstall
  needed -- ruled this out as a hypothesis once, don't re-check it.
- **`scripts/goose_quicktest.sh` needs a lane number for concurrent runs** --
  each lane gets its own Chrome port/profile; without distinct lanes, two
  concurrent runs fight over the same Chrome instance.
