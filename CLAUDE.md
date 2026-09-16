# ApplyPilot

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
