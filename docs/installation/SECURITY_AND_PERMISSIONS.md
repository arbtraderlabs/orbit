# ORBIT Security and Permissions

ORBIT is an early-stage project. The code paths described here are under review; run the clean-room pilot in issue #7 before using them on a repository with valuable work.

## Trust boundaries

- GitHub issues, repository files, worker output, and command manifests are untrusted data. A ticket proposes scope; only an authenticated operator can admit it.
- Admission is bound to the canonical request hash, repository and issue identity, source commit, body hash, policy hash, and expiry.
- Worker completion records `DONE`; a separate authenticated approver must inspect the pinned diff and record `ACCEPTED`.
- Draft PR delivery verifies the already-pushed branch against the accepted per-file hashes and never merges or deploys.
- The worker receives a temporary HOME and a small environment allowlist. It still runs as the current OS user, with that user's filesystem access. This is not an OS sandbox. Run only operator-trusted executables.
- Worker worktrees, logs, receipts, and the SQLite ledger are retained. ORBIT does not clean them automatically.

## Local file permissions

Store `~/.config/orbit/policy.json`, command manifests, the SQLite ledger, worker receipts, and test output under the operator account. The policy and command manifests must be owned by that account and not group/world writable; manifests and receipts are required to be private (mode `0600`). The ledger parent directory and worker state directory are private (`0700`). Keep all operator policy and state outside the target checkout.

The builder and test command manifests are JSON argv arrays, not shell scripts. Their executable path must be absolute. The command runs with a temporary HOME and no inherited environment variables; do not put credentials in command arguments or files visible to the worker.

## GitHub access

The current CLI uses an existing authenticated `gh` session. It does not create credentials or install a GitHub App. Limit access to the single target repository when possible.

For a fine-grained token or GitHub App used with all current ORBIT GitHub API paths, the relevant repository permissions are:

| Permission | Level | Used for |
| --- | --- | --- |
| Issues | Read and write | Verify/create task issues, inspect parent relationships, read and upsert informational issue comments |
| Pull requests | Read and write | Reconcile existing PRs and create a draft PR after acceptance |
| Contents | Read | Verify pinned commits and compare the remote branch with accepted file hashes |
| Metadata | Read | Repository identity; GitHub grants this read permission by default |

The CLI does not need repository administration, Actions, secrets, workflow, organization membership, or Contents write permission for its API operations. The operator separately pushes a reviewed branch; that Git operation is outside ORBIT and requires the operator's normal Git access.

There is no ORBIT GitHub App manifest, installation flow, or app-key handling implementation yet. The current runtime delegates authentication to the operator's `gh` session. Do not create or install an ORBIT App for this draft based only on the permission table above; App support must be implemented and reviewed before a supported installation guide can be published.

GitHub can require organization approval or SSO even when a token has the permissions above. Start with a public disposable repository and verify access before using a private repository. Consult GitHub's [fine-grained token permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens), [issue API](https://docs.github.com/en/rest/issues/issues), [pull request API](https://docs.github.com/en/rest/pulls/pulls), and [commit comparison API](https://docs.github.com/en/rest/commits/commits#compare-two-commits) when configuring an installation.

## Services and scheduling

ORBIT does not install a system service, timer, background process, or startup task. Do not add a systemd timer to run `orbit worker` unattended yet. The live pilot, host reboot recovery, log rotation, and OS-level process isolation have not been validated.

## Emergency stop

Set the repository's operator policy `admission.stop` to `true` to stop new admissions, worker claims, and lease heartbeats. A running worker checks the policy while executing and terminates its process group when it sees STOP or a policy change. The operator may still record completion or inspect status. STOP is not a substitute for host process control if the machine is compromised.
