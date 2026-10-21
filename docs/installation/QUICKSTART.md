# ORBIT Bootstrap and Ticket Generator Quickstart

Requirements: Python 3.11+, Git; GitHub CLI is required only for authenticated ticket creation. No CrashDash dependency.

From ORBIT checkout:

```bash
python -m pip install -e .
mkdir -p /tmp/my-unrelated-project
orbit init --repo example/my-unrelated-project --target /tmp/my-unrelated-project
orbit validate --target /tmp/my-unrelated-project
orbit doctor --target /tmp/my-unrelated-project
python -m unittest discover -s tests -v
```

For an existing unrelated checkout, replace target with its directory and repo with its GitHub owner/name. Init refuses to overwrite an existing `.orbit` directory. Phase 1 only creates local configuration and runs diagnostics.

## Create an authenticated GitHub task (Phase 2)

Ticket creation requires an authenticated `gh` session and an operator policy file stored outside the target repository. The policy is separate from repository-controlled config so a repository edit cannot add an authorized creator.

```bash
gh auth login
mkdir -p ~/.config/orbit
cat > ~/.config/orbit/policy.json <<'JSON'
{
  "schema_version": 1,
  "repositories": {
    "owner/project": {
      "ticket_creators": ["your-github-login"],
      "parent_issue": 12,
      "admission": {
        "approvers": ["your-github-login"],
        "stop": false,
        "limits": {
          "admissions_per_day": 30,
          "worker_attempts_per_day": 30,
          "max_concurrent": 1,
          "max_seconds_per_attempt": 600,
          "max_retries": 2,
          "max_request_lifetime_seconds": 3600,
          "max_ticket_age_seconds": 604800
        }
      }
    }
  }
}
JSON
chmod 600 ~/.config/orbit/policy.json
cat > /tmp/orbit-task.json <<'JSON'
{
  "schema_version": 1,
  "title": "Implement a scoped task",
  "objective": "Add the requested feature.",
  "scope": {
    "allowed_paths": ["src/feature/"],
    "prohibited_changes": ["No deployment or credential changes"]
  },
  "acceptance_tests": ["The feature tests pass."],
  "dependencies": "None",
  "escalation_boundaries": "Ask before adding credentials or expanding permissions."
}
JSON
orbit ticket create \
  --target /path/to/project \
  --request /tmp/orbit-task.json \
  --output /tmp/orbit-request.json
```

The target must be a Git checkout whose `origin` matches its configured GitHub repository. The command verifies the authenticated GitHub user, repository identity, ticket creator, issue body and operator-configured parent issue (when set), then records the current commit SHA, complete ticket scope, and body hash. The policy file belongs to the operator, not the repository. It emits a `PROPOSED` request only. Ticket creation does not authorize execution, admit a job, or dispatch an agent.

## Explicit admission (Phase 2)

Configure the operator policy, including the admission approvers and limits, **before** creating the ticket. The policy hash is pinned into the ticket request; changing policy means creating a new request. Admission is allowed only when `stop` is explicitly `false`. The operator reviews the request JSON, then runs the command once to obtain its canonical hash and again with that exact hash:

```bash
# Prints a confirmation command and hash, but does not admit anything.
orbit admit --target /path/to/project --request /tmp/orbit-request.json

# Copy the hash from the message after reviewing the request details.
orbit admit \
  --target /path/to/project \
  --request /tmp/orbit-request.json \
  --confirm-request-sha256 <reviewed-request-hash> \
  --expires-seconds 900
```

The command verifies the authenticated approver, exact repository and source commit, unchanged issue body and creator, current policy hash, ticket age, configured parent, STOP, UTC daily budget, concurrency, and confirmation hash. It records an `ADMITTED` receipt in a private SQLite audit file under the operator state directory. Admission is not execution; no worker is dispatched.

## Inspect and lease admitted work (Phase 3 foundation)

The durable queue records admitted receipts, attempts, state transitions, UTC worker-attempt budgets, and private lease tokens in the same SQLite ledger. It exposes lease operations for a future worker adapter; these commands do not launch an agent or run repository code.

```bash
orbit queue status --target /path/to/project
orbit queue claim --target /path/to/project --worker-id local-worker --claim-key <stable-unique-key>
orbit queue heartbeat --target /path/to/project --request-id <request-id> --lease-token <lease-token>
orbit queue finish --target /path/to/project --request-id <request-id> --lease-token <lease-token> --outcome succeeded --summary "evidence reference"
```

Claims are idempotent for the same worker and stable claim key while the lease is live. Lease expiry is recoverable on the next claim; retries and attempt duration are bounded by the admission policy pinned in the receipt. STOP prevents new claims and lease heartbeats. A worker may still record a completed attempt while STOP is active. `queue status` reports admissions, job states, attempt outcomes, and rejected admission validations separately.

## Run one bounded worker attempt (Phase 3 foundation)

`orbit worker run` claims one admitted job, creates a detached worktree at the pinned source commit outside the checkout, runs one builder argv and one test argv, checks changed paths against `allowed_paths`, and writes a private receipt containing command, output, diff, and source hashes. Existing worktrees and dirty files in the target checkout are left in place. Worker worktrees and their evidence are retained for operator review; ORBIT does not clean them up.

The two command manifests are private JSON arrays of arguments. Their executable must be an absolute path and commands are invoked without a shell. For example, to exercise a local stub:

```bash
cat > /tmp/orbit-builder.json <<'JSON'
["/usr/bin/python3", "-c", "from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/result.txt').write_text('built\\n')"]
JSON
cat > /tmp/orbit-tests.json <<'JSON'
["/usr/bin/python3", "-c", "from pathlib import Path; assert Path('src/result.txt').read_text() == 'built\\n'"]
JSON
chmod 600 /tmp/orbit-builder.json /tmp/orbit-tests.json
orbit worker run \
  --target /path/to/project \
  --worker-id local-stub \
  --claim-key <stable-unique-key> \
  --command-json /tmp/orbit-builder.json \
  --test-command-json /tmp/orbit-tests.json
```

The subprocess environment contains a temporary HOME and a small allowlist; the operator's credentials and environment variables are not inherited. The command still runs as the current OS user and this is not an OS sandbox. Use only an operator-trusted executable. STOP or a policy change terminates the process group; a failed test is retryable within policy, while an out-of-scope diff is blocked. Each attempt is limited to 20 MiB of combined command output and 100 MiB of collected diff evidence. The receipt stores hashes instead of stdout/stderr content; those streams are retained in the private worker evidence directory.
