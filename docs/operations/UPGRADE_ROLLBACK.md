# ORBIT Upgrade and Rollback

ORBIT is still under review. Back up the operator ledger and preserve worker artifacts before changing versions. No automatic rollback or destructive worktree cleanup is provided.

## Locate operator state

Unless `XDG_CONFIG_HOME` or `XDG_STATE_HOME` are set, the operator policy is `~/.config/orbit/policy.json`, the admission/queue ledger is `~/.local/state/orbit/admissions.sqlite3`, worker worktrees are under `~/.local/state/orbit/worker/worktrees/`, and worker receipts/output are under `~/.local/state/orbit/worker/receipts/` and `~/.local/state/orbit/worker/worktrees/`.

Keep these paths outside the target repository. Protect the policy and ledger with private file permissions; keep the containing directories private.

## Back up before upgrade

Pause new worker attempts by setting operator STOP. Wait for a running attempt to finish or let its bounded lease expire. Use SQLite's online backup API rather than copying a live database file while WAL mode is active:

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path

source = Path.home() / ".local/state/orbit/admissions.sqlite3"
backup = source.with_name("admissions-before-upgrade.sqlite3")
if not source.is_file():
    raise SystemExit(f"ledger does not exist: {source}")
if backup.exists():
    raise SystemExit(f"backup already exists; choose a new path: {backup}")
with sqlite3.connect(source) as current, sqlite3.connect(backup) as copy:
    current.backup(copy)
backup.chmod(0o600)
print(backup)
PY
```

Also preserve the operator policy, command manifests, receipts, logs, target checkout and every retained ORBIT worktree. Confirm the backup opens with `sqlite3` or Python before proceeding.

## Upgrade

Use a fresh virtual environment and install a reviewed source revision or release artifact. Do not upgrade an active environment in place while a worker is running. Verify the installed version, run the complete test suite, inspect queue status, and run the one-minute local demo before re-enabling new attempts.

Back up the operator policy before editing it. Reconcile it with the current [operator policy schema](../architecture/operator-policy-v1.schema.json); older policies may lack the worker-attempt, per-attempt duration, or retry limits required by queue/worker commands. ORBIT does not rewrite operator policy automatically. If `XDG_STATE_HOME` is set, change the ledger path in the backup command below to match it.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e /path/to/reviewed/orbit-checkout
python -m unittest discover -s /path/to/reviewed/orbit-checkout/tests -v
```

Current migrations are additive `CREATE TABLE IF NOT EXISTS` operations. No command drops or rewrites old admission, job, attempt, event, or review rows. Keep the backup even when the new version starts successfully.

## Roll back

1. Set operator STOP and do not start another attempt.
2. Preserve all current files and record the installed revision plus `orbit queue status` output.
3. Activate the previous reviewed environment or install the previous reviewed revision into a new environment.
4. Do not restore an old ledger over a newer one unless the recovery plan explicitly requires it; a newer ledger may contain valid attempt or review evidence.
5. If restoration is required, move the current ledger and its `-wal`/`-shm` companions aside as a set, then restore the verified backup while ORBIT is stopped.
6. Inspect the restored queue and retained worker worktrees before clearing STOP.

Do not delete worktrees to make rollback appear clean. Do not use `git worktree prune`; unresolved worktrees contain operator-reviewable work and evidence.
