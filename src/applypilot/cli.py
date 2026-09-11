"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    rescore: bool = typer.Option(
        False, "--rescore",
        help="Re-score every job with a description, not just unscored ones. "
             "Use after changing scoring criteria (e.g. pay floors).",
    ),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        rescore=rescore,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    backend: Optional[str] = typer.Option(
        None, "--backend", "-b",
        help="Engine that drives the browser: 'goose' (Goose CLI on a cheap "
             "OpenRouter model) or 'claude' (Claude Code CLI, uses your "
             "subscription quota). Defaults to apply_backend in settings.json.",
    ),
    fallback: Optional[str] = typer.Option(
        None, "--fallback",
        help="Backend to retry a job on when the primary one gives up. "
             "Defaults to apply_fallback_backend in settings.json. "
             "Pass 'none' to disable.",
    ),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    queued: Optional[str] = typer.Option(
        None, "--queued",
        help="Apply to the jobs in this queue batch, in the order they were "
             "selected. Set by the web UI; ignores --min-score and the other "
             "ranked-queue filters, because a person already chose these.",
    ),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Chrome + an apply backend)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: there is actually something to apply to. For a queued batch the
    # question is whether that batch has anything left in it, not whether the
    # ranked queue does -- a batch can be perfectly valid while the ranked
    # queue is empty, and vice versa.
    if queued:
        conn = get_connection()
        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue_batch = ? AND apply_status = 'queued'",
            (queued,),
        ).fetchone()[0]
        if pending == 0:
            console.print(f"[red]Batch {queued} has no jobs waiting.[/red]")
            raise typer.Exit(code=1)
    elif not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    if limit is not None:
        effective_limit = limit
    elif queued:
        # The batch is the limit. Defaulting to 1 here would apply to the first
        # job the user picked and silently drop the other nineteen.
        effective_limit = 0
    else:
        effective_limit = 0 if continuous else 1

    from applypilot.config import load_settings
    from applypilot.apply.backends import BACKEND_NAMES

    _settings = load_settings()
    effective_backend = (backend or _settings.get("apply_backend", "goose")).lower()
    if effective_backend not in BACKEND_NAMES:
        console.print(
            f"[red]Unknown backend {effective_backend!r}.[/red] "
            f"Expected one of: {', '.join(BACKEND_NAMES)}"
        )
        raise typer.Exit(code=1)

    raw_fallback = fallback if fallback is not None else _settings.get("apply_fallback_backend")
    effective_fallback = (raw_fallback or "").strip().lower() or None
    if effective_fallback in ("none", "off"):
        effective_fallback = None
    if effective_fallback and effective_fallback not in BACKEND_NAMES:
        console.print(
            f"[red]Unknown fallback backend {effective_fallback!r}.[/red] "
            f"Expected one of: {', '.join(BACKEND_NAMES)}, or 'none'"
        )
        raise typer.Exit(code=1)
    if effective_fallback == effective_backend:
        effective_fallback = None

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Backend:  {effective_backend}")
    if effective_backend == "goose":
        # --model selects a Claude model and is meaningless here; Goose's model
        # is goose_model in settings.json. Printing "haiku" just misleads.
        from applypilot.config import DEFAULTS as _D
        _gm = _settings.get("goose_model") or _D["goose_model"]
        _gp = _settings.get("goose_provider") or _D["goose_provider"]
        console.print(f"  Model:    {_gm} [dim]({_gp})[/dim]")
    else:
        console.print(f"  Model:    {model}")
    console.print(f"  Fallback: {effective_fallback or '[dim]none[/dim]'}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    if queued:
        console.print(f"  Batch:    {queued}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        backend=effective_backend,
        fallback_backend=effective_fallback,
        queue_batch=queued,
    )


@app.command()
def serve(
    port: int = typer.Option(8420, "--port", help="Port to listen on."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open a browser."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)."),
) -> None:
    """Open the web UI for browsing jobs and choosing which to apply to.

    Bound to 127.0.0.1 by default. There is no --host flag: this serves your
    resumes and drives Chrome profiles holding your logged-in sessions, so it
    is not something to expose on a network. Set APPLYPILOT_HOST to bind to a
    private overlay-network interface (e.g. a Tailscale IP) instead -- never
    to 0.0.0.0 or a public interface.
    """
    _bootstrap()

    from applypilot.web.server import HOST, serve as run_server

    url = f"http://{HOST}:{port}"
    console.print(f"\n[bold blue]ApplyPilot[/bold blue]  {url}")
    console.print("[dim]Loopback only. Ctrl+C to stop.[/dim]\n")

    if not no_open:
        # Fire the browser slightly late so the server is accepting by the
        # time the tab asks for the page.
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        run_server(port=port, reload=reload)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


def _is_stage_running(stage: str) -> bool:
    """Whether an `applypilot run <stage>` process is alive right now.

    Plain `ps` grep rather than a pidfile/lock -- every stage here is a
    short-lived CLI subprocess the overnight/hourly scripts spawn and let
    exit, not a daemon that manages its own lock file, so this is the only
    signal available without adding new bookkeeping. macOS/Linux only.
    """
    try:
        out = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5,
        ).stdout
        needle = f"applypilot run {stage}"
        return any(
            needle in line and "grep" not in line
            for line in out.splitlines()
        )
    except Exception:
        return False


def _build_status_renderables(stats: dict) -> list:
    """Build the Rich renderables for `status` -- shared by the one-shot
    print and the --watch live-refresh loop so they never drift apart.
    """
    renderables: list = []

    # Pipeline activity -- last-seen timestamps plus whether each stage's
    # process is actually running right now, so this answers "is anything
    # happening" without needing a separate `ps aux` by hand.
    activity = Table(title="Pipeline Activity", show_header=True, header_style="bold blue")
    activity.add_column("Stage")
    activity.add_column("Status", justify="center")
    activity.add_column("Last activity")

    enrich_running = _is_stage_running("enrich")
    score_running = _is_stage_running("score")
    activity.add_row(
        "Enrich", "[green]running[/green]" if enrich_running else "[dim]idle[/dim]",
        stats.get("last_enrich_at") or "never",
    )
    activity.add_row(
        "Score", "[green]running[/green]" if score_running else "[dim]idle[/dim]",
        stats.get("last_score_at") or "never",
    )
    for site, last_discovered in stats.get("last_discovered_by_site", []):
        activity.add_row(f"Discover ({site or 'unknown'})", "", last_discovered or "never")
    renderables.append(activity)

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))
    renderables.append(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="Score Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        renderables.append(dist_table)

    # Score distribution split internship vs new_grad (7+ only -- these are
    # the tiers that actually compete for apply slots).
    if stats.get("score_distribution_by_type"):
        by_type: dict[str, dict[int, int]] = {}
        for job_type, score, count in stats["score_distribution_by_type"]:
            by_type.setdefault(job_type, {})[score] = count

        total_scored_by_type = stats.get("total_scored_by_type", {})

        type_table = Table(title="Top-Tier Score by Job Type (7+)", show_header=True, header_style="bold cyan")
        type_table.add_column("Job Type")
        for s in (10, 9, 8, 7):
            type_table.add_column(str(s), justify="right")
        type_table.add_column("Total 7+", justify="right", style="bold")
        type_table.add_column("Total Scored", justify="right", style="dim")

        for job_type in sorted(by_type):
            row_counts = [by_type[job_type].get(s, 0) for s in (10, 9, 8, 7)]
            type_table.add_row(
                job_type, *[str(c) for c in row_counts], str(sum(row_counts)),
                str(total_scored_by_type.get(job_type, 0)),
            )

        renderables.append(type_table)

    if stats.get("terminal_internships"):
        renderables.append(
            f"[bold green]Terminal internships (no return-to-school required, "
            f"guaranteed top apply priority):[/bold green] {stats['terminal_internships']}"
        )

    if stats.get("likely_terminal_internships"):
        renderables.append(
            f"[yellow]Likely-terminal internships (strong match, but posting "
            f"never says either way -- worth applying with your real grad date, "
            f"not queue-boosted):[/yellow] {stats['likely_terminal_internships']}"
        )

    if stats.get("remote_spring_internships"):
        renderables.append(
            f"[bold green]Remote spring internships (term ends before "
            f"graduation, fully remote -- same top-priority tier as terminal "
            f"internships):[/bold green] {stats['remote_spring_internships']}"
        )

    # By site
    if stats["by_site"]:
        site_table = Table(title="Jobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        renderables.append(site_table)

    return renderables


@app.command(name="rescore-stale")
def rescore_stale(
    limit: int = typer.Option(0, "--limit", "-n",
                              help="Cap how many jobs to re-score (0 = all)."),
) -> None:
    """Re-score jobs that predate the TERM and TERMINAL EVIDENCE checks.

    Those rows are identifiable exactly -- they have a scored_at but a NULL
    `term` -- and they are the great majority of the corpus. Until they are
    re-scored, the Spring/Summer internship gate falls back to reading the
    title, and the terminal-internship path (what makes a Summer role
    reachable at all once you've graduated) can barely fire.

    Runs the full recompute chain afterwards, so eligibility, desirability
    and the company tiers all land in the same pass.
    """
    _bootstrap()
    from applypilot.scoring.scorer import run_scoring

    result = run_scoring(limit=limit, stale_only=True)
    typer.echo(f"Re-scored {result['scored']} jobs "
               f"({result['errors']} errors) in {result['elapsed']:.0f}s")
    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command(name="recompute")
def recompute() -> None:
    """Recompute every derived field -- no LLM calls, so this is free.

    Desirability, company tiers, eligibility tighteners and the terminal /
    remote-spring flags are all pure arithmetic and SQL over columns that are
    already stored, which is the whole reason re-tuning weights or editing
    the big-tech company list never costs a re-score.
    """
    _bootstrap()
    from applypilot.scoring import scorer

    typer.echo(f"desirability:  {scorer.compute_desirability()}")
    typer.echo(f"company tiers: {scorer.compute_company_tiers()}")
    typer.echo(f"grad-date elig: {scorer.recompute_eligibility_for_grad_date()}")
    typer.echo(f"term elig:      {scorer.recompute_eligibility_for_unwanted_term()}")
    typer.echo(f"terminal:       {scorer.compute_terminal_internships()}")
    typer.echo(f"likely terminal:{scorer.compute_likely_terminal_internships()}")
    typer.echo(f"company pattern:{scorer.compute_company_pattern_terminal()}")
    typer.echo(f"company excl:   {scorer.compute_company_pattern_non_terminal()}")
    typer.echo(f"evidence hints: {scorer.compute_terminal_evidence_hints()}")
    typer.echo(f"remote spring:  {scorer.compute_remote_spring_internships()}")


@app.command(name="requeue-boilerplate")
def requeue_boilerplate() -> None:
    """Re-queue jobs whose stored description is an aggregator's UI chrome,
    not the actual posting.

    A Jobright.ai bug: its own JSON-LD occasionally carries a promotional
    blurb ("Customize Your Resume... Analyze How Well You Fit...") as the
    JobPosting `description` field instead of real content. Enrichment used
    to accept that at face value -- short but over the 50-char floor -- and
    the scorer would then compute a fit_score against nothing, with no
    company extracted either. The extraction cascade now rejects that text
    at every tier and prefers the real employer page when Jobright's own
    "Original Job Post" link resolves, but rows already scraped before that
    fix need to be reset to pick it up. Only clears detail_scraped_at,
    full_description and detail_attempts; run `applypilot run enrich`
    afterwards to actually re-scrape them.
    """
    _bootstrap()
    from applypilot.enrichment.detail import requeue_boilerplate_rows

    n = requeue_boilerplate_rows()
    typer.echo(f"Re-queued {n} job(s) for re-enrichment. Run `applypilot run enrich` to fetch them.")


@app.command()
def status(
    watch: bool = typer.Option(False, "--watch", "-w", help="Auto-refresh live instead of a one-shot snapshot."),
    interval: float = typer.Option(5.0, "--interval", help="Seconds between refreshes in --watch mode."),
) -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    if not watch:
        console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")
        for r in _build_status_renderables(get_stats()):
            console.print(r)
            console.print()
        return

    console.print("[dim]Watching -- Ctrl+C to stop[/dim]")
    try:
        with Live(console=console, refresh_per_second=1, screen=True) as live:
            while True:
                group = Group(
                    "[bold]ApplyPilot Pipeline Status[/bold] "
                    f"[dim](refreshing every {interval:.0f}s, {time.strftime('%H:%M:%S')})[/dim]",
                    "",
                    *_build_status_renderables(get_stats()),
                )
                live.update(group)
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


@app.command(name="ats-stats")
def ats_stats_cmd() -> None:
    """Show per-ATS run cost, duration, and token stats (Workday, Greenhouse, ...)."""
    _bootstrap()

    from applypilot import costs

    rows = costs.ats_stats()
    if not rows:
        console.print("[dim]No completed apply runs recorded yet.[/dim]")
        return

    table = Table(title="Per-ATS Apply Stats", show_header=True, header_style="bold cyan")
    table.add_column("ATS")
    table.add_column("Backend")
    table.add_column("Runs", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Median cost", justify="right")
    table.add_column("Total cost", justify="right")
    table.add_column("Median time", justify="right")
    table.add_column("Median turns", justify="right")
    table.add_column("Median in tok", justify="right")
    table.add_column("Median out tok", justify="right")
    table.add_column("Median cache tok", justify="right")

    def _fmt(v, suffix: str = "", digits: int = 0) -> str:
        return "-" if v is None else f"{v:.{digits}f}{suffix}"

    for r in rows:
        table.add_row(
            r["ats"], r["backend"], str(r["n_runs"]),
            f"{r['success_rate'] * 100:.0f}%",
            _fmt(r["median_cost_usd"], digits=3),
            f"${r['total_cost_usd']:.2f}",
            _fmt(r["median_duration_s"], "s", 0),
            _fmt(r["median_llm_requests"], digits=0),
            _fmt(r["median_input_tokens"], digits=0),
            _fmt(r["median_output_tokens"], digits=0),
            _fmt(r["median_cache_read_tokens"], digits=0),
        )

    console.print(table)


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path, DEFAULTS,
        load_settings,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    # Mirror llm._detect_provider()'s precedence exactly: LLM_URL wins over both
    # key-based providers. Checking GEMINI_API_KEY first reported "Gemini" for a
    # setup that was really routing every call to the LLM_URL endpoint.
    if has_local:
        model = os.environ.get("LLM_MODEL", "local-model")
        results.append(("LLM API key", ok_mark,
                        f"{os.environ.get('LLM_URL')} ({model})"))
        if has_gemini or has_openai:
            results.append(("LLM_URL override", warn_mark,
                            "LLM_URL is set, so GEMINI_API_KEY/OPENAI_API_KEY are ignored"))
    elif has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-3.1-flash-lite")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Apply backend: whichever one is actually configured is the one that has
    # to be present. The other is reported as the fallback it is.
    _s = load_settings()
    primary = (_s.get("apply_backend") or "goose").lower()
    fallback = (_s.get("apply_fallback_backend") or "").lower() or None

    def _role(name: str) -> str:
        if name == primary:
            return "apply backend"
        if name == fallback:
            return "apply fallback"
        return "unused"

    # Goose CLI
    goose_bin = shutil.which("goose")
    goose_role = _role("goose")
    if goose_bin:
        results.append(("Goose CLI", ok_mark, f"{goose_bin} ({goose_role})"))
    elif goose_role == "unused":
        results.append(("Goose CLI", "[dim]optional[/dim]", "not the configured backend"))
    else:
        results.append(("Goose CLI", fail_mark if goose_role == "apply backend" else warn_mark,
                        f"{goose_role}; install from "
                        "https://block.github.io/goose/docs/getting-started/installation/"))

    # OpenRouter key -- what Goose runs on
    if primary == "goose" or fallback == "goose":
        if os.environ.get("OPENROUTER_API_KEY"):
            model = _s.get("goose_model") or DEFAULTS["goose_model"]
            results.append(("OpenRouter key", ok_mark, f"Goose model: {model}"))
        else:
            results.append(("OpenRouter key", fail_mark,
                            "Goose needs OPENROUTER_API_KEY in ~/.applypilot/.env "
                            "(https://openrouter.ai/keys)"))

    # Claude Code CLI
    claude_bin = shutil.which("claude")
    claude_role = _role("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, f"{claude_bin} ({claude_role})"))
    elif claude_role == "unused":
        results.append(("Claude Code CLI", "[dim]optional[/dim]", "not the configured backend"))
    else:
        results.append(("Claude Code CLI",
                        fail_mark if claude_role == "apply backend" else warn_mark,
                        f"{claude_role}; install from https://claude.ai/code"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Chrome + Node.js, plus "
                      "Goose+OpenRouter key or the Claude Code CLI)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Chrome + Node.js, plus "
                      "Goose+OpenRouter key or the Claude Code CLI)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
