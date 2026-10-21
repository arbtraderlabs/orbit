# ORBIT Release Readiness Checklist

This is a preparation checklist, not release authorization. Public launch and merge require explicit operator approval. Do not mark any item complete without the linked evidence.

## Product and security evidence

- [ ] Merge the reviewed Phase 0–3 implementation PRs after independent code review.
- [ ] Complete issue #7's clean-room pilot in a disposable unrelated GitHub repository. Record issue, admission, worker attempt, test evidence, accepted diff, branch commit, and draft PR references.
- [ ] Repeat the pilot for timeout, STOP, crash/restart, daily budgets, exhausted retries, changed policy, out-of-scope files, and GitHub API failure/reconciliation.
- [ ] Review the worker's current OS-user execution model. Do not claim OS isolation until a sandbox is designed and tested.
- [ ] Verify least-privilege GitHub App/token permissions against the APIs used; document installation, revocation, and organization approval.
- [ ] Confirm no credentials, generated secrets, customer data, or local state are present in the repository or CI artifacts.
- [ ] Run independent security review of path scope, symlink handling, worktree behavior, command environment, evidence hashes, and local ledger permissions.
- [ ] Confirm `DONE` and `ACCEPTED` remain separate and a worker cannot create acceptance, merge, or deploy.

## Release engineering

- [ ] CI passes on every supported Python version and operating system.
- [ ] Build and inspect the wheel and source distribution from a clean checkout.
- [ ] Verify installation, upgrade, backup, and rollback using the published instructions.
- [ ] Run `scripts/orbit-one-minute-demo.sh` from the release candidate.
- [ ] Ensure README, examples, schemas, and permission documentation describe only tested behavior.
- [ ] Confirm STOP defaults, admission budgets, retry limits, log retention, and artifact locations.
- [ ] Review license, dependency inventory, vulnerability reports, changelog, and release notes.
- [ ] Record the exact release commit and immutable artifact hashes.

## Authorization gate

- [ ] Operator explicitly authorizes merge and release.
- [ ] Operator explicitly authorizes any GitHub App installation or expanded permissions.
- [ ] Operator explicitly authorizes publication and any production deployment (ORBIT V1 does not deploy).

Until the clean-room pilot and independent review are complete, describe ORBIT as an early-stage draft implementation. Do not present the demo as an autonomous production workflow.
