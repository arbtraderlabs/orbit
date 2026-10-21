# ORBIT Implementation Plan and Acceptance

## Phase 0 — specification (this change)
- Standalone architecture and explicit trust boundaries.
- Versioned bootstrap configuration, ticket schema and documented state machine.
- GitHub issue backlog with dependencies and concrete acceptance.
- Decision: Python standard-library-first CLI for zero-dependency bootstrap; runtime dependencies may be added later via ADR.

## Phase 1 — portable bootstrap (this change)
- `python -m orbit init --repo owner/name --target /path/to/repo` writes `.orbit/config.json` and `.orbit/AGENT_BOOTSTRAP.md` without replacing existing files.
- `python -m orbit doctor --target /path/to/repo` checks config, Git, Python, workspace and optional gh CLI; never requires credentials for local validation.
- `python -m orbit validate --target /path/to/repo` validates version, repo slug, limits and guardrails; fails closed.
- Tests exercise empty unrelated repository, existing-file refusal, invalid repository, unsafe settings, missing config, and fresh-process CLI invocation.
- Installation via `pip install -e .` from source, or `python -m orbit` from checkout.

## Phase 2 — governed GitHub tickets and admission
`#2` creates a versioned, pinned PROPOSED request through an authenticated `gh api` principal, after checking an operator-owned policy file outside the target checkout. It verifies repository identity, Git origin, issue creator/body and optional native sub-issue parent; the request carries the scoped ticket, source SHA and policy/body hashes. `#3` requires an allowlisted authenticated approver and typed canonical request hash, re-verifies the live issue and source commit, rejects stale requests/policy, checks STOP, UTC admission budget, concurrency and expiry, then records an ADMITTED receipt in a private local SQLite audit ledger. Neither step dispatches a worker.

## Phase 3 — durable worker
`#4` adds the SQLite job/attempt/event ledger, atomic admission-to-queue insertion, idempotent per-worker claims, bounded renewable leases, UTC attempt budgets, retries, expiry recovery, and separate validation counts. An expired lease is recovered the next time a claim is requested.

`#5` adds an explicit argv-only builder/test runner in a detached worktree at the pinned source SHA, temporary HOME/minimal environment, periodic policy/lease checks, runtime deadline, allowed-path enforcement, and private hashed evidence receipts. It retains worktrees and never cleans the target. The process still runs as the operator's OS user; this is not an OS sandbox.

`#6` adds distinct AUTHORISED/ADMITTED/RUNNING/DONE/ACCEPTED reporting, authenticated independent acceptance bound to exact diff and receipt hashes, idempotent issue status comments, and draft PR creation only after acceptance and remote file verification. ORBIT does not commit or push the worker worktree. Remaining: independent model review, live disposable-repository rehearsal, and richer evidence browsing. Do not enable execution for untrusted binaries.

## Phase 4 — hardening and launch
Negative security tests, host reboot recovery, observability, installer, troubleshooting, upgrade/rollback, demonstration and public release. Do not claim production readiness without independent evidence.

## V2 — independent review
Codex reads immutable builder evidence; review PASS/FAIL/UNKNOWN; corrective task roundtrip to builder; maximum two corrections by default; human+ChatGPT final review. No model self-approval or auto-merge.

## Phase 1 acceptance
Run `python -m unittest discover -s tests -v` and `python -m orbit doctor --target <fresh-repo>`. Phase 1 does not dispatch, install services or execute agents. Phase 2 may create authenticated, pinned GitHub tickets; ticket creation is still only PROPOSED and cannot authorize or dispatch execution.
