"""Webshare swap-until-good loop, with the API and IPQS stubbed."""
import pytest

from applypilot import config
from applypilot.apply import webshare


def _px(*ips):
    return [{"proxy_address": ip, "port": 8000, "username": "u", "password": "p"} for ip in ips]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(webshare, "STATE", tmp_path / "swaps.json")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("KEEP=1\nAPPLY_PROXY_0=old\nAPPLY_PROXY=home\n")
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(webshare.time, "sleep", lambda s: None)
    return tmp_path


def _wire(monkeypatch, scores, pools, abuse={}):
    """pools: successive pool() results; scores: ip -> score."""
    it = iter(pools)
    cur = {"p": next(it)}
    monkeypatch.setattr(webshare, "pool", lambda: cur["p"])
    monkeypatch.setattr(webshare, "_call", lambda *a, **k: cur.update(p=next(it)) or {})
    monkeypatch.setattr(webshare.ip_health, "fraud_score", lambda ip, max_age=None: scores[ip])
    monkeypatch.setattr(webshare.ip_health, "abuse_score", lambda ip, max_age=None: abuse.get(ip))


def test_swaps_until_good_and_writes_env(env, monkeypatch):
    _wire(monkeypatch, {"1.1.1.1": 90, "2.2.2.2": 10, "3.3.3.3": 70, "4.4.4.4": 5},
          [_px("1.1.1.1", "2.2.2.2"),          # initial
           _px("2.2.2.2", "3.3.3.3"),          # 1.1.1.1 -> 3.3.3.3 (bad)
           _px("2.2.2.2", "4.4.4.4")])         # 3.3.3.3 -> 4.4.4.4 (good)
    px = webshare.ensure(log=lambda m: None)
    assert [p["proxy_address"] for p in px] == ["2.2.2.2", "4.4.4.4"]
    st = webshare.state()
    assert st["remaining"] == webshare.QUOTA - 2
    assert [(s["old_ip"], s["old_score"], s["new_ip"], s["new_score"]) for s in st["swaps"]] == [
        ("1.1.1.1", 90, "3.3.3.3", 70), ("3.3.3.3", 70, "4.4.4.4", 5)]
    txt = (env / ".env").read_text()
    assert "KEEP=1" in txt and "APPLY_PROXY=home" in txt and "APPLY_PROXY_0=old" not in txt
    assert "APPLY_PROXY_0=2.2.2.2:8000:u:p" in txt and "APPLY_PROXY_1=4.4.4.4:8000:u:p" in txt
    assert "APPLY_PROXY_7=4.4.4.4:8000:u:p" in txt  # 2 proxies -> round-robin over 8 slots


def test_no_swap_when_quota_spent_or_score_unknown(env, monkeypatch):
    webshare._save({"month": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m"), "remaining": 0, "quota": 48, "swaps": []})
    _wire(monkeypatch, {"1.1.1.1": 99, "2.2.2.2": None}, [_px("1.1.1.1", "2.2.2.2")])
    px = webshare.ensure(log=lambda m: None)
    assert len(px) == 2 and webshare.state()["remaining"] == 0


def test_abuse_score_is_second_gate_and_fallback(env, monkeypatch):
    # IPQS says clean but AbuseIPDB says abusive -> bad; IPQS unavailable -> AbuseIPDB decides
    _wire(monkeypatch, {"1.1.1.1": 5, "9.9.9.9": None}, [_px("1.1.1.1")],
          abuse={"1.1.1.1": 60, "9.9.9.9": 2})
    assert webshare.check("1.1.1.1") == (5, True)
    assert webshare.check("9.9.9.9") == (2, False)


def test_non_us_ip_is_swapped_without_scoring(env, monkeypatch):
    foreign = [{**_px("1.1.1.1")[0], "country_code": "DE"}]
    _wire(monkeypatch, {"2.2.2.2": 5}, [foreign, _px("2.2.2.2")])
    px = webshare.ensure(log=lambda m: None)
    assert [p["proxy_address"] for p in px] == ["2.2.2.2"]


def test_on_block_rescores_and_swaps_bad_ip_only(env, monkeypatch):
    monkeypatch.setattr(webshare, "enabled", lambda: True)
    monkeypatch.setenv("APPLY_PROXY_0", "1.1.1.1:8000:u:p")
    scores = {"1.1.1.1": 90, "2.2.2.2": 5}
    _wire(monkeypatch, scores, [_px("1.1.1.1"), _px("2.2.2.2")])
    webshare.on_block(0, log=lambda m: None)
    assert "APPLY_PROXY_0=2.2.2.2:8000:u:p" in (env / ".env").read_text()
    # a clean IP with one block is kept; three blocks force a swap
    webshare._strikes.clear()
    monkeypatch.setenv("APPLY_PROXY_0", "2.2.2.2:8000:u:p")
    _wire(monkeypatch, {"2.2.2.2": 5, "3.3.3.3": 5}, [_px("2.2.2.2"), _px("3.3.3.3")])
    webshare.on_block(0, log=lambda m: None); webshare.on_block(0, log=lambda m: None)
    assert "2.2.2.2" in (env / ".env").read_text()
    webshare.on_block(0, log=lambda m: None)
    assert "APPLY_PROXY_0=3.3.3.3:8000:u:p" in (env / ".env").read_text()


def test_before_job_swaps_only_a_bad_ip_and_ignores_others(env, monkeypatch):
    monkeypatch.setattr(webshare, "enabled", lambda: True)
    monkeypatch.setenv("APPLY_PROXY_0", "1.1.1.1:8000:u:p")
    monkeypatch.setenv("APPLY_PROXY_1", "9.9.9.9:1:u:p")  # e.g. the home relay
    webshare._ours.clear(); webshare._ours.add("1.1.1.1")
    _wire(monkeypatch, {"1.1.1.1": 90, "2.2.2.2": 5}, [_px("1.1.1.1"), _px("2.2.2.2")])
    webshare.before_job(1, log=lambda m: None)   # not a pool IP: untouched
    assert webshare.state()["remaining"] == webshare.QUOTA
    webshare.before_job(0, log=lambda m: None)   # bad pool IP: swapped
    assert "APPLY_PROXY_0=2.2.2.2:8000:u:p" in (env / ".env").read_text()
    assert webshare.state()["remaining"] == webshare.QUOTA - 1
