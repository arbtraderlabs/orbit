# ORBIT Phase 1 Quickstart

Requirements: Python 3.11+, Git recommended; GitHub CLI optional. No CrashDash dependency.

From ORBIT checkout:

```bash
python -m pip install -e .
mkdir -p /tmp/my-unrelated-project
orbit init --repo example/my-unrelated-project --target /tmp/my-unrelated-project
orbit validate --target /tmp/my-unrelated-project
orbit doctor --target /tmp/my-unrelated-project
python -m unittest discover -s tests -v
```

For an existing unrelated checkout, replace target with its directory and repo with its GitHub owner/name. Init will refuse to overwrite an existing .orbit/config.json or agent bootstrap. The Phase 1 CLI only creates local configuration and runs diagnostics. It does not authenticate, create issues, dispatch agents, open PRs or schedule work. Those are tracked as later-phase GitHub issues.
