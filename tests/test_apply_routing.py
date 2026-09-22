"""Company lock and ATS-aware routing across workers sharing proxy IPs."""

import pytest

from applypilot.apply import launcher, routing
from applypilot.database import init_db

GH = "https://boards.greenhouse.io/{}/jobs/{}"
ASHBY = "https://jobs.ashbyhq.com/{}/00000000-0000-0000-0000-00000000000{}"
WD = "https://{}.wd5.myworkdayjobs.com/en-US/x/job/y_R-{}"


def _job(conn, url, company, pos, status="queued", agent=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, "
        "tailored_resume_path, fit_score, apply_status, queue_batch, "
        "queue_position, queued_at, agent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (url, "SWE Intern", "x", company, url, "/tmp/r", 9, status, "b", pos,
         "2026-01-01", agent))
    conn.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "t.db")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: conn)
    routing._claims.clear()
    launcher._busy.clear()  # acquire_job marks workers busy; isolate tests
    for k in [f"APPLY_PROXY_{i}" for i in range(9)]:
        monkeypatch.delenv(k, raising=False)
    # workers 0-2 share IP A, 3-5 share IP B
    for w in range(6):
        ip = "1.1.1.1" if w < 3 else "2.2.2.2"
        monkeypatch.setenv(f"APPLY_PROXY_{w}", f"{ip}:8000:u:p")
    return conn


def _claim(w):
    r = launcher.acquire_job(manual_queue=True, worker_id=w)
    return r and r["url"]


def test_same_company_never_on_two_workers_even_same_ip(db):
    _job(db, GH.format("acme", 1), "Acme", 0)
    _job(db, GH.format("acme", 2), "Acme", 1)
    _job(db, ASHBY.format("beta", 1), "Beta", 2)
    assert _claim(0) == GH.format("acme", 1)
    # worker 1 shares worker 0's IP: must skip the other Acme row
    assert _claim(1) == ASHBY.format("beta", 1)
    # worker 4 is on another IP: company lock is global, still nothing for Acme
    assert _claim(4) is None


def test_prefers_different_ats_within_ip(db):
    _job(db, GH.format("a", 1), "A", 0)
    _job(db, GH.format("b", 1), "B", 1)
    _job(db, ASHBY.format("c", 1), "C", 2)
    assert _claim(0) == GH.format("a", 1)
    assert _claim(1) == ASHBY.format("c", 1)   # skips GH B: worker 0 is on GH
    assert _claim(2) == GH.format("b", 1)      # worst case: same ATS is fine


def test_other_ip_may_take_same_ats(db):
    _job(db, GH.format("a", 1), "A", 0)
    _job(db, GH.format("b", 1), "B", 1)
    assert _claim(0) == GH.format("a", 1)
    assert _claim(3) == GH.format("b", 1)      # different IP: no penalty


def test_spreads_ats_across_ips(db):
    # IP A already claimed a Workday; B has not -> B takes Workday next
    _job(db, WD.format("w1", 1), "W1", 0)
    _job(db, GH.format("g1", 1), "G1", 1)
    _job(db, WD.format("w2", 2), "W2", 2)
    assert _claim(0) == WD.format("w1", 1)
    db.execute("UPDATE jobs SET apply_status='applied', agent_id=NULL WHERE apply_status='in_progress'")
    db.commit()
    assert _claim(0) == GH.format("g1", 1)     # A has done WD, prefers GH
    assert _claim(3) == WD.format("w2", 2)     # B has done neither, takes queue head


def test_ip_key_groups_by_host_port(db, monkeypatch):
    assert routing.ip_key(0) == routing.ip_key(2) != routing.ip_key(3)
    assert routing.ip_key(8) == "direct"


def test_socks5_forwarder_and_config(monkeypatch):
    """Static socks5:// proxy parses, and the forwarder tunnels HTTP + CONNECT through a no-auth SOCKS5 stub."""
    import asyncio, socket, threading, urllib.request
    from applypilot import config
    from applypilot.apply import proxy_forwarder

    monkeypatch.setenv("APPLY_PROXY_PUBLIC_HOST", "vm.example")
    p = config._parse_proxy_string("socks5://100.1.1.1:1080")
    assert p["socks5"] and p["capsolver"] == "" and p["port"] == "1080"
    assert "vm.example" not in config._parse_proxy_string("9.9.9.9:1:u:p")["capsolver"]

    seen = []

    async def stub(r, w):  # minimal SOCKS5 server that answers as an origin
        await r.readexactly(3); w.write(b"\x05\x00")
        head = await r.readexactly(5); name = await r.readexactly(head[4]); await r.readexactly(2)
        seen.append(name); w.write(b"\x05\x00\x00\x01" + bytes(6))
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"); await w.drain(); w.close()

    def free():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port
    sp, lp = free(), free()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def serve():
        await asyncio.start_server(stub, "127.0.0.1", sp); ready.set(); await asyncio.sleep(5)
    threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True).start()
    ready.wait(2)
    stop = proxy_forwarder.start_forwarder(lp, "127.0.0.1", sp, "", "", socks5=True)
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{lp}"}))
        assert opener.open("http://example.test/x", timeout=3).read() == b"ok"
        assert seen == [b"example.test"]
    finally:
        stop()


def test_queued_worker_exits_when_queue_empty_and_home_worker_waits_for_primaries(db, monkeypatch):
    """A --queued run is one launch: workers exit on an empty queue, and the
    home-fallback worker keeps polling until every primary is done."""
    import sqlite3, threading
    from applypilot.apply.dashboard import init_worker
    path = db.execute("PRAGMA database_list").fetchone()[2]

    def conn_per_thread(*a, **k):  # sqlite connections are thread-bound
        c = sqlite3.connect(path, timeout=5); c.row_factory = sqlite3.Row; return c
    monkeypatch.setattr(launcher, "get_connection", conn_per_thread)
    init_worker(0); init_worker(8)
    launcher._stop_event.clear(); launcher._primaries_done.clear()
    assert launcher.worker_loop(worker_id=0, limit=0, manual_queue=True) == (0, 0)

    launcher.POLL_INTERVAL = 0.05
    t = threading.Thread(target=launcher.worker_loop,
                         kwargs=dict(worker_id=8, limit=0, home_fallback=True), daemon=True)
    t.start(); t.join(0.4)
    assert t.is_alive(), "home worker must keep polling while primaries may still feed it"
    launcher._primaries_done.set()
    t.join(2)
    assert not t.is_alive(), "home worker must exit once primaries are done and backlog is empty"
    launcher._primaries_done.clear()


def test_idle_worker_stays_alive_while_a_peer_is_busy(db, monkeypatch):
    """Queued run: an idle worker keeps polling while any peer holds a job,
    and exits once nobody does."""
    import sqlite3, threading
    from applypilot.apply.dashboard import init_worker
    path = db.execute("PRAGMA database_list").fetchone()[2]

    def conn_per_thread(*a, **k):
        c = sqlite3.connect(path, timeout=5); c.row_factory = sqlite3.Row; return c
    monkeypatch.setattr(launcher, "get_connection", conn_per_thread)
    monkeypatch.setattr(launcher, "IDLE_POLL", 0.05)
    init_worker(0)
    launcher._stop_event.clear(); launcher._busy.clear(); launcher._busy.add(5)  # a busy peer
    t = threading.Thread(target=launcher.worker_loop,
                         kwargs=dict(worker_id=0, limit=0, manual_queue=True), daemon=True)
    t.start(); t.join(0.4)
    assert t.is_alive(), "must wait while a peer is busy"
    launcher._busy.discard(5)
    t.join(2)
    assert not t.is_alive(), "must exit once every worker is idle and the queue is empty"
