# ORBIT Troubleshooting

## `orbit` command is missing

Run from an ORBIT checkout with `PYTHONPATH=src python3 -m orbit ...`, or install the package into an isolated environment. Use Python 3.11 or newer. On Debian/Ubuntu, install the distribution's `python3-venv` package before creating a virtual environment if `ensurepip` is unavailable. Avoid modifying the system Python installation.

## `origin` does not match the configured repository

`orbit init --repo owner/name` stores the expected GitHub repository. Ticket, admission, queue, worker, review, report, and delivery operations require the target's `origin` URL to point to that exact `owner/name`. Inspect it with:

```bash
git -C /path/to/project remote -v
```

Correct the remote or initialize the intended checkout. Do not bypass the identity check.

## GitHub API authentication or permission errors

The ticket, admission, review, report publishing, and draft delivery commands use the existing GitHub CLI session. Check it with `gh auth status`. Authenticate interactively with `gh auth login` if the operator has already approved that account; ORBIT does not store or create credentials.

For fine-grained tokens or GitHub Apps, see [Security and Permissions](../installation/SECURITY_AND_PERMISSIONS.md). Also check repository access, organization approval, SSO, and whether the authenticated login appears in the external policy's `ticket_creators` or `admission.approvers` list. A public repository may permit unauthenticated reads, but writes still require an authorized identity.

## Admission asks for `--confirm-request-sha256`

Read the complete request JSON and verify its repository, issue, source SHA, allowed paths, prohibited changes, acceptance tests, and escalation boundaries. Re-run the command with the printed hash only after review. Any change to the request changes the hash. A policy change requires a new request.

## Admission rejects the issue or source as stale

Admission checks the current target HEAD, live GitHub issue body and creator, issue state, parent relationship (when configured), policy hash, ticket age, and expiry. Create a fresh request from the current checkout instead of editing the old request or bypassing verification.

## No job is returned by `orbit queue claim`

Check `orbit queue status --target /path/to/project`. The request may not be admitted, may have expired, may be blocked by a policy change, or may have exhausted retry/attempt budgets. Claims are limited by UTC-day counters and policy concurrency.

## A job remains `RUNNING` after interruption

The next claim after lease expiry records the attempt as `INTERRUPTED` and either queues a bounded retry or marks the job `BLOCKED`. Do not reuse the previous claim key or lease token. Inspect the event history and retained worktree before retrying.

## Worker terminates before completion

The worker stops at its per-attempt deadline, admission expiry, 20 MiB combined output cap, 100 MiB evidence cap, STOP/policy change, or failed lease heartbeat. Review the private worker output directory and retained detached worktree. The target checkout and existing worktrees are preserved. If the attempt failed due to a transient command/test failure, policy allows only the configured retry count.

## Worker changes are blocked as out of scope

The runner compares all tracked and untracked paths with the admitted `allowed_paths`. It blocks an attempt that adds, changes, or deletes a path outside that scope. Review the retained worktree; do not widen the request by editing its ticket. Create a new reviewed request if scope must change.

## Review or draft PR creation is rejected

Review requires a successful `DONE` attempt, intact private evidence receipt, exact `diff_sha256`, and an authenticated approver distinct from the ticket creator and admission approver. Draft delivery also requires the same authenticated reviewer, a matching review receipt hash, an already-pushed branch with exact accepted file hashes, and a base branch ancestral to the pinned source. Re-run status locally, inspect the pinned worker evidence, and resolve the specific mismatch; never force delivery.

## GitHub update timed out

Status comments and draft PRs carry stable ORBIT markers. Re-run the same `orbit report status --publish` or `orbit deliver create-pr` command. ORBIT looks for the marker and verifies the remote result before retrying creation.

## Unexpected state or corrupt local ledger

Stop worker execution by setting operator STOP. Preserve the ledger file, WAL/SHM files, worker worktrees, receipts, and command output for review. Do not delete the ledger or run `git worktree prune`. Take a consistent SQLite backup as described in [Upgrade and Rollback](UPGRADE_ROLLBACK.md) before attempting repair.
