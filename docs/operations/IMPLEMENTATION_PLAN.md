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

## Phase 2 — governed GitHub tickets
Authenticated `gh api` principal checked against an operator-owned policy file outside the target checkout; ticket generator; repository identity and Git origin verification; pinned source SHA; body hash; optional native GitHub sub-issue parent linkage; machine-readable PROPOSED request. The request records the authenticated creator and is explicitly not an admission receipt. Issue creation alone must never authorize or claim dispatch. Admission, expiry, replay prevention and policy binding are Phase 2 follow-up work in #3.

## Phase 3 — durable worker
SQLite ledger, queue leases, agent adapters, worktree isolation, timeout, STOP, budgets, retries, crash recovery, diff/test evidence, draft PR. Test with a totally unrelated disposable GitHub repo; verify issue -> authorised -> admitted -> worker -> commit -> draft PR.

## Phase 4 — hardening and launch
Negative security tests, host reboot recovery, observability, installer, troubleshooting, upgrade/rollback, demonstration and public release. Do not claim production readiness without independent evidence.

## V2 — independent review
Codex reads immutable builder evidence; review PASS/FAIL/UNKNOWN; corrective task roundtrip to builder; maximum two corrections by default; human+ChatGPT final review. No model self-approval or auto-merge.

## Phase 1 acceptance
Run `python -m unittest discover -s tests -v` and `python -m orbit doctor --target <fresh-repo>`. Phase 1 does not dispatch, install services or execute agents. Phase 2 may create authenticated, pinned GitHub tickets; ticket creation is still only PROPOSED and cannot authorize or dispatch execution.
