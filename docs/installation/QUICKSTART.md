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
      "parent_issue": 12
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

The target must be a Git checkout whose `origin` matches its configured GitHub repository. The command verifies the authenticated GitHub user, repository identity, ticket creator, issue body and operator-configured parent issue (when set), then records the current commit SHA and body hash. The policy file belongs to the operator, not the repository. It emits a `PROPOSED` request only. Ticket creation does not authorize execution, admit a job, or dispatch an agent. Admission is a separate later phase.
