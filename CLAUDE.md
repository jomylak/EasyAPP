# ApplyPilot

## Tests (CI runs these on every push/PR — `.github/workflows/ci.yml`)

- Unit: `pytest tests/ --ignore=tests/e2e` and `ruff check src/`.
- E2E: `pytest tests/e2e` boots the real server on a seeded temp DB, fuzzes
  the API with schemathesis (any 5xx fails) and clicks through the UI with
  Playwright (any JS/console error or 5xx fails).
- Frontend: `cd web && npm run lint && npm run build` (build = typecheck).

When you change code:
- **Bug fix → first write a test that fails, then fix it.** The test is the
  proof the bug existed and the guard against it coming back.
- **New logic (a branch, parser, SQL filter, state transition) → add a test**
  in the matching `tests/test_<module>.py`. Trivial one-liners don't need one.
- **New API endpoint or query param →** bound it (`Query(ge=, le=)`) and let
  `tests/e2e/test_api_fuzz.py` cover it. If it starts real work (spawns
  processes, touches Gmail/Chrome/credentials), add it to the exclusion
  lists in `test_api_fuzz.py` *and* `test_ui_smoke.py`'s `BLOCKED`.
- Tests must never read the real `~/.applypilot` — CI has none. Monkeypatch
  `config` paths/loaders or use `tmp_path`.
- Run the relevant tests before calling a change done; deploy only on green.

## Deploying to the live VM

The live pipeline and web dashboard run on Oracle Cloud (`ssh oracle-applypilot`,
Tailscale IP `100.84.247.84`), not locally. **After making a code change,
push it to the VM** with:

```
bash scripts/deploy_to_vm.sh
```

This rsyncs the working tree and restarts `applypilot-serve.service` and
`applypilot-pipeline.service`. Do this proactively once a change is
verified locally (build/typecheck/import passes) — don't wait to be asked.

### Careful: don't kill a live run

`applypilot-serve.service` restarts on every deploy, and it spawns each
`applypilot apply` run as a detached child process (see
`src/applypilot/web/server.py`'s module docstring). If that unit's
`KillMode` is ever back to the systemd default (`control-group`), a
restart kills the *entire cgroup* — including that in-flight apply run and
its Chrome workers — not just the server. The unit should have
`KillMode=process` set specifically so a redeploy only touches the server
process itself. Before redeploying, it's worth a quick check that this is
still in place (`ssh oracle-applypilot "systemctl cat applypilot-serve.service | grep KillMode"`)
and, if a run looks like it's actually in flight, sanity-check the deploy
won't cut it off rather than assuming it's safe.
