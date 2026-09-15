"""Run a real apply-worker Chrome instance against known fingerprinting
test sites and save a comparable report -- CreepJS trust score, sannysoft's
bot-check table, and the raw WebGL renderer/vendor strings.

Reuses applypilot.apply.chrome.launch_chrome so the browser is launched
exactly the way production workers are (same flags, same stealth extension,
same profile setup) -- this measures the real pipeline, not a hand-rolled
approximation of it.

Usage:
    python scripts/fingerprint_check.py --label vm-direct
    python scripts/fingerprint_check.py --label vm-proxy --via-proxy
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from applypilot.config import load_env, ensure_dirs

load_env()
ensure_dirs()

from applypilot.apply import chrome  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REPORTS_DIR = Path(__file__).parent / "fingerprint_reports"

CREEPJS_URL = "https://abrahamjuliot.github.io/creepjs/"
SANNYSOFT_URL = "https://bot.sannysoft.com/"

WEBGL_JS = """
() => {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (!gl) return {renderer: null, vendor: null};
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    if (!ext) return {renderer: gl.getParameter(gl.RENDERER), vendor: gl.getParameter(gl.VENDOR)};
    return {
        renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),
        vendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
    };
}
"""

CREEPJS_RESULT_JS = """
() => document.body.innerText.slice(0, 12000)
"""

SANNYSOFT_ROWS_JS = """
() => {
    const rows = [];
    document.querySelectorAll("table tr").forEach(tr => {
        const cells = [...tr.querySelectorAll("td, th")].map(td => td.textContent.trim());
        if (cells.length) rows.push(cells);
    });
    return rows;
}
"""


async def run(label: str, via_proxy: bool, worker_id: int, extra_args: list[str] | None = None) -> dict:
    proc = chrome.launch_chrome(worker_id, use_proxy=via_proxy, extra_args=extra_args)
    port = chrome.BASE_CDP_PORT + worker_id
    report: dict = {"label": label, "via_proxy": via_proxy, "timestamp": time.time()}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()

            report["webgl"] = await page.evaluate(WEBGL_JS)

            await page.goto(SANNYSOFT_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1500)
            report["sannysoft_rows"] = await page.evaluate(SANNYSOFT_ROWS_JS)

            await page.goto(CREEPJS_URL, wait_until="load", timeout=30000)
            # CreepJS's scoring is async and has no reliable completion event;
            # it finishes well within this window on a normal connection. Its
            # trust gauge is canvas-drawn (not scrapeable text), so pull the
            # concrete numeric signals it does print as plain text instead --
            # the "N% like headless" heuristic and the leaked WebRTC IP.
            await page.wait_for_timeout(12000)
            body_text = await page.evaluate(CREEPJS_RESULT_JS)
            headless_match = re.search(r"(\d{1,3})% like headless", body_text)
            webrtc_ip_match = re.search(r"foundation/ip:.*?\n.*?\nip: ([\d.]+)", body_text, re.S)
            report["creepjs"] = {
                "pct_like_headless": int(headless_match.group(1)) if headless_match else None,
                "webrtc_leaked_local_ip": webrtc_ip_match.group(1) if webrtc_ip_match else None,
                "body_excerpt": body_text,
            }

            await page.close()
            await browser.close()
    finally:
        chrome.cleanup_worker(worker_id, proc)
        if via_proxy:
            chrome.release_proxy_slot()

    return report


def summarize(report: dict) -> str:
    lines = [f"label={report['label']} via_proxy={report['via_proxy']}"]
    webgl = report.get("webgl") or {}
    lines.append(f"WebGL renderer: {webgl.get('renderer')!r} vendor: {webgl.get('vendor')!r}")
    creepjs = report.get("creepjs") or {}
    lines.append(f"CreepJS 'like headless': {creepjs.get('pct_like_headless')}%")
    lines.append(f"WebRTC leaked local IP: {creepjs.get('webrtc_leaked_local_ip')}")
    fails = [row for row in report.get("sannysoft_rows", []) if any("failed" in c.lower() for c in row)]
    lines.append(f"sannysoft flagged rows: {fails if fails else 'none'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="check", help="Run label, e.g. vm-direct, vm-proxy")
    parser.add_argument("--via-proxy", action="store_true", help="Route through APPLY_PROXY")
    parser.add_argument("--worker-id", type=int, default=99, help="Worker id to use for the throwaway profile")
    parser.add_argument("--swiftshader", action="store_true",
                         help="Force software WebGL via --use-angle=swiftshader (experiment for GPU-less VMs)")
    args = parser.parse_args()

    if args.via_proxy:
        if not chrome.acquire_proxy_slot():
            print("Could not acquire the single APPLY_PROXY slot.", file=sys.stderr)
            sys.exit(1)

    extra_args = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] if args.swiftshader else None
    report = asyncio.run(run(args.label, args.via_proxy, args.worker_id, extra_args))

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"{int(report['timestamp'])}-{args.label}.json"
    out_path.write_text(json.dumps(report, indent=2))

    print(summarize(report))
    print(f"\nFull report: {out_path}")


if __name__ == "__main__":
    main()
