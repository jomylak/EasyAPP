"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config
from applypilot.apply import proxy_forwarder

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Local forwarding-proxy port base — each worker uses BASE_PROXY_PORT + worker_id
BASE_PROXY_PORT = 9322

# Loaded into every worker's Chrome -- see stealth_extension/inject.js.
_STEALTH_EXTENSION_DIR = Path(__file__).parent / "stealth_extension"

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
# Track each worker's *current job's* upstream proxy string (CapSolver format,
# "type:host:port:user:pass") and forwarder-stop callable, keyed by worker_id.
# Multiple workers run as threads in the same process (see launcher.worker_loop's
# ThreadPoolExecutor), so this must be worker_id-scoped, not a bare module global.
_worker_proxies: dict[int, str] = {}
_worker_forwarder_stops: dict[int, callable] = {}
_chrome_lock = threading.Lock()

# APPLY_PROXY is one home relay on one real IP -- it can only ever carry one
# worker's traffic at a time, or concurrent proxied sessions would just be
# multiple workers competing for (and overloading) that single connection.
# Serializes launch_chrome(use_proxy=True) across every worker thread; see
# acquire_proxy_slot/release_proxy_slot below.
_proxy_lock = threading.Lock()


def acquire_proxy_slot(stop_event=None, poll: float = 1.0) -> bool:
    """Block until the single APPLY_PROXY slot is free, then hold it.

    Caller must call release_proxy_slot() once done with the proxied Chrome
    (after cleanup_worker, in the same try/finally that launched it) --
    holding this is not tied to launch_chrome itself since the slot must stay
    held for the whole proxied job, not just the launch.

    Polls in `poll`-second slices (rather than blocking indefinitely) so a
    worker waiting for the slot still notices `stop_event` and can give up.
    Returns False if stop_event was set before the slot became free.
    """
    while True:
        if _proxy_lock.acquire(timeout=poll):
            return True
        if stop_event is not None and stop_event.is_set():
            return False


def release_proxy_slot() -> None:
    """Release the slot acquired by acquire_proxy_slot()."""
    _proxy_lock.release()


def get_worker_proxy(worker_id: int) -> str | None:
    """The CapSolver-format proxy string ("type:host:port:user:pass") this
    worker's currently-launched Chrome is using, or None if APPLY_PROXY isn't
    configured, or if this Chrome was launched with use_proxy=False (the
    default -- see launch_chrome's docstring). CapSolver must solve through
    the exact same egress IP Chrome is browsing from -- see get_apply_proxy's
    docstring for why.
    """
    with _chrome_lock:
        return _worker_proxies.get(worker_id)


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _wait_for_port_free(port: int, timeout: float = 10.0) -> bool:
    """Block until nothing is listening on a port.

    Killing a process is not the same as it having exited: Chrome takes a moment
    to release the port and its profile lock. Relaunching into that window
    produces an instance whose DevTools endpoint dies partway through a run,
    which is indistinguishable from a crash from the agent's point of view.
    """
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket()
        try:
            sock.settimeout(0.5)
            sock.connect(("127.0.0.1", port))
        except OSError:
            return True  # refused == nothing listening
        finally:
            sock.close()
        time.sleep(0.25)
    logger.warning("Port %d still in use after %.0fs", port, timeout)
    return False


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile.
    # Chrome is always launched with --profile-directory=Default (see below), so only
    # that one profile's contents are ever needed -- cloning the whole user-data root
    # (which holds every other Chrome profile the user has) wastes gigabytes per worker.
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}" / "Default"
        if candidate.exists():
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data() / "Default"

    dst_default = profile_dir / "Default"
    dst_default.mkdir(parents=True, exist_ok=True)

    if source.exists():
        logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                    worker_id, source)

        # Copy essential profile dirs -- skip caches and heavy transient data
        skip = {
            "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
            "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
            "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
            "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
            "SingletonLock", "SingletonSocket", "SingletonCookie",
            # Chrome is launched with --disable-extensions, so extension
            # data is never read -- skip it to avoid wasting space.
            "Extensions", "Local Extension Settings", "Extension State",
            "Sync Extension Settings",
        }

        for item in source.iterdir():
            if item.name in skip:
                continue
            dst = dst_default / item.name
            try:
                if item.is_dir():
                    shutil.copytree(
                        str(item), str(dst), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            "Cache", "Code Cache", "GPUCache", "Service Worker",
                        ),
                    )
                else:
                    shutil.copy2(str(item), str(dst))
            except (PermissionError, OSError):
                pass  # skip locked files

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def _wait_for_cdp(port: int, worker_id: int, proc: subprocess.Popen,
                  timeout: float = 20.0) -> bool:
    """Block until Chrome's DevTools endpoint actually accepts connections.

    This previously slept a flat 3 seconds, but Chrome typically does not open
    the port until ~4s, so whatever connected next could race it. The Claude
    path masked this because Playwright MCP takes a while to boot; a backend
    connects over CDP immediately and lost the race with
    "connect ECONNREFUSED 127.0.0.1:<port>".

    Returns:
        True if the endpoint answered, False if it never came up in time.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.error("[worker-%d] Chrome exited during startup (rc=%s)",
                         worker_id, proc.returncode)
            return False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)

    logger.warning("[worker-%d] Chrome DevTools port %d did not open within %.0fs",
                   worker_id, port, timeout)
    return False


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False, use_proxy: bool = False,
                  extra_args: list[str] | None = None) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        use_proxy: Route this Chrome instance through APPLY_PROXY (if
            configured). Default False -- every job starts on a direct
            connection. launcher.py's worker loop only passes True for the
            one relaunch it does after a job's first attempt hits a captcha
            wall, so the home-relay proxy only ever carries the traffic of
            jobs that actually need CapSolver's IP to match Chrome's, instead
            of every job on every worker regardless of whether it ever sees
            a captcha.
        extra_args: Additional Chrome command-line flags, appended after the
            standard set. For one-off experiments (e.g. scripts/fingerprint_check.py
            trying `--use-angle=swiftshader`) without changing every worker's
            default launch args.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port, and wait for it
    # to actually release the port before launching into the same one.
    _kill_on_port(port)
    _wait_for_port_free(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    # A fresh sticky-session proxy per job: this worker's Chrome and the
    # CAPTCHA solve for whatever job it runs must share one egress IP, since
    # the whole point is making the solve session and the submitting session
    # look like the same real user. Only requested when the caller passes
    # use_proxy=True -- APPLY_PROXY being set is necessary but not
    # sufficient; see this function's docstring for why it's opt-in per
    # launch rather than automatic just because the env var exists.
    proxy = None
    if use_proxy:
        import secrets
        session_id = f"w{worker_id}-{secrets.token_hex(4)}"
        proxy = config.get_apply_proxy(session_id)

    _stop_forwarder_for_worker(worker_id)
    proxy_port = None
    if proxy:
        proxy_port = BASE_PROXY_PORT + worker_id
        stop = proxy_forwarder.start_forwarder(
            local_port=proxy_port,
            upstream_host=proxy["host"],
            upstream_port=proxy["port"],
            user=proxy["user"],
            passwd=proxy["pass"],
        )
        with _chrome_lock:
            _worker_proxies[worker_id] = proxy["capsolver"]
            _worker_forwarder_stops[worker_id] = stop
    else:
        with _chrome_lock:
            _worker_proxies.pop(worker_id, None)

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1920,1080",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # setup_worker_profile() already skips copying Extensions/Local
        # Extension Settings/Extension State when cloning the profile, so the
        # inherited-extensions problem this used to guard against (a rival
        # autofill extension injecting its own "Upload Resume"/"Autofill"
        # controls into the page, confusing a vision-driven agent and eating
        # ~40% of the viewport) can't recur even without --disable-extensions.
        # Load exactly our own stealth extension instead of blocking all
        # extensions outright -- see stealth_extension/inject.js for what it
        # does and why. --disable-extensions-except scopes this down to just
        # that one extension, so nothing else can load even if a future
        # profile-cloning change stops excluding them.
        f"--load-extension={_STEALTH_EXTENSION_DIR}",
        f"--disable-extensions-except={_STEALTH_EXTENSION_DIR}",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
        # Real Chrome connected over CDP still exposes navigator.webdriver and
        # other automation tells to fingerprinting scripts (reCAPTCHA v3,
        # Ashby's own bot check) by default. This is the one free mitigation;
        # it does not need a vendor decision the way the proxy below does.
        "--disable-blink-features=AutomationControlled",
        # WebRTC negotiates over raw UDP and ignores --proxy-server entirely,
        # so a page can probe ICE candidates and leak the real local/host IP
        # straight past APPLY_PROXY's HTTP(S) tunnel. This restricts WebRTC to
        # the configured proxy (or drops it if there is none), closing that
        # side channel independent of whether a job is proxied.
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if proxy_port:
        cmd.append(f"--proxy-server=127.0.0.1:{proxy_port}")
    if headless:
        cmd.append("--headless=new")
    if extra_args:
        cmd.extend(extra_args)

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    _wait_for_cdp(port, worker_id, proc)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def _stop_forwarder_for_worker(worker_id: int) -> None:
    """Stop and forget this worker's local proxy forwarder, if any."""
    with _chrome_lock:
        stop = _worker_forwarder_stops.pop(worker_id, None)
        _worker_proxies.pop(worker_id, None)
    if stop:
        stop()


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    _stop_forwarder_for_worker(worker_id)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()
        worker_ids = list(_worker_forwarder_stops.keys())

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    for wid in worker_ids:
        _stop_forwarder_for_worker(wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()
        worker_ids = list(_worker_forwarder_stops.keys())

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    for wid in worker_ids:
        _stop_forwarder_for_worker(wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
