"""The apply run must start as its own systemd unit so a server restart can't kill it."""
import subprocess
from types import SimpleNamespace

from applypilot.web import server


def test_systemd_run_wraps_command_in_own_unit():
    wrapped = server._systemd_run_cmd(["python", "-m", "x"], "/tmp/l.log")
    assert wrapped[:4] == ["sudo", "-n", "systemd-run", "--quiet"]
    assert f"--unit={server.RUN_UNIT}" in wrapped
    assert wrapped[-3:] == ["python", "-m", "x"]


def test_falls_back_to_child_process_when_systemd_run_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/systemd-run")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="denied")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: SimpleNamespace(pid=4242))
    assert server._spawn_detached(["true"], None, tmp_path / "l.log") == 4242
    assert calls and calls[0][2] == "systemd-run"


def test_returns_main_pid_from_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=0, stdout="777\n" if cmd[0] == "systemctl" else "", stderr=""))
    assert server._spawn_detached(["true"], None, tmp_path / "l.log") == 777
