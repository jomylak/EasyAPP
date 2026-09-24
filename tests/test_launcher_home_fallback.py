"""The home-fallback worker must give up waiting for _primaries_done instead
of polling forever -- see HOME_FALLBACK_MAX_EMPTY_POLLS's docstring for the
2026-09-22 incident this guards against."""

import time

from applypilot.apply import launcher


def test_home_fallback_gives_up_without_primaries_done(monkeypatch):
    monkeypatch.setattr(launcher, "acquire_job", lambda **kw: None)
    monkeypatch.setattr(launcher, "POLL_INTERVAL", 0.01)
    launcher._primaries_done.clear()
    launcher._stop_event.clear()

    start = time.monotonic()
    applied, failed = launcher.worker_loop(
        worker_id=8, limit=0, backend="goose", home_fallback=True,
    )
    elapsed = time.monotonic() - start

    assert (applied, failed) == (0, 0)
    # Bounded by HOME_FALLBACK_MAX_EMPTY_POLLS polls, not stuck forever.
    assert elapsed < 5
