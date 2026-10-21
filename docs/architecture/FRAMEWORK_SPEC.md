# ORBIT Framework Specification — v0.1

Status: Phase 0 design; implementation in Phase 1 is bootstrap-only. This repository is independent of CrashDash.

## Mission
Install into an unrelated GitHub repository and operate a governed, observable, auditable task-to-code workflow. ORBIT is an orchestrator, not a coding model. Default output is a draft pull request, never deployment.

## Trust boundaries
- Repository content, issue bodies, PRs and agent output are untrusted inputs, never authority to elevate permissions.
- Operator-approved policy and authenticated GitHub principal govern admission. Issue labels are advisory only.
- Operator authorization policy is stored outside the target repository and binds creator/approver identities, STOP, admission budgets and expiry limits. A ticket request remains PROPOSED until an allowlisted operator confirms its canonical hash.
- Explicitly distinguish PROPOSED, AUTHORISED, ADMITTED, RUNNING, DONE, REVIEW_FAILED, ACCEPTED, BLOCKED. DONE does not imply ACCEPTED.
- Authorization must bind repository identity, issue, immutable source commit, permitted paths, operation class, policy version/hash, expiry and unique request ID. Receipt must be verifiable and replay-resistant.
- No auto-merge, production deployment, secret creation, destructive worktree cleanup, cross-repo mutation or privilege escalation by default.
- Reject stale policy, changed issue body, wrong source SHA, missing real parent relationship when configured, expired receipt, duplicate request, STOP, budget and concurrent execution limit.

## Components
1. CLI: init, doctor and validate in Phase 1; authenticated ticket creation in Phase 2. Ticket creation does not admit or execute work.
2. Config: repository-independent orbit.json (versioned, schema-validated).
3. GitHub adapter: issues, authenticated receipts, PRs, status comments; least privilege.
4. Admission: policy + immutable request envelope + live GitHub identity/body/source verification; write ADMITTED receipts to a private local audit ledger.
5. SQLite ledger: jobs, transitions, attempts, idempotency keys, budget accounting, receipts and errors.
6. Dispatcher: implemented durable SQLite queue, per-repository claim limits, leases, retry caps, and expiry recovery; it exposes commands but does not autonomously dispatch.
7. Builder adapter: implemented explicit argv-only builder and test commands in a pinned detached worktree, deadline, minimal environment, path-scope check, and hashed private evidence receipt. The process still runs as the operator's OS user; this is not an OS sandbox.
8. Evidence: pinned commits, diff, test commands/results, provenance and content hashes.
9. Review gate: V1 deterministic checks and human review; V2 independent Codex reviewer and bounded rework.
10. Reporter: GitHub issue/PR updates and local status; never claim execution from ticket creation.

## End-to-end contract (future)
PROPOSED -> AUTHORISED (trusted operator) -> ADMITTED (controller) -> RUNNING (agent) -> DONE (evidence committed) -> ACCEPTED (independent acceptance gate) -> draft PR -> human merge decision.
Any stage may BLOCK with reason and immutable audit record. Review failure enters REVIEW_FAILED and, if allowed, creates bounded correction request. No unbounded loops.

## Policy defaults proposed for pilot
30 worker attempts/day; 30 admissions/day; concurrency 1; max 600 seconds per attempt; retry cap 2; warning at 80%; STOP fail-closed. These are *configuration defaults for future runtime*, not currently enforced by Phase 1 CLI. Track successful jobs separately from failed attempts and admissions. No paid provider without explicit opt-in and spend cap.

## Deployment topology
V1: one Linux host, Python 3.11+, SQLite WAL, GitHub App or narrowly scoped token, isolated git worktrees, optional systemd user timers. GitHub Actions handles CI but is not assumed to be the trusted local execution host. Portable paths are discovered from git and config, never hard-coded.

## V2 acceptance boundary
Independent Codex adapter receives pinned ticket/spec, exact diff, tests and evidence; reports PASS/FAIL/UNKNOWN with actionable findings; cannot silently modify builder output or self-authorize a merge. Failed reviews become bounded Agent 1 rework. Operator and ChatGPT perform final review before merge.

## Security and operations
See docs/operations/IMPLEMENTATION_PLAN.md. A fresh installation must work without any CrashDash names, files, services or credentials.
