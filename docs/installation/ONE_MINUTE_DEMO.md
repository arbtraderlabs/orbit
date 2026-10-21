# ORBIT One-Minute Local Demo

From the root of a checkout with Python 3.11+, Git, and the test suite:

```bash
./scripts/orbit-one-minute-demo.sh
```

The script creates a new temporary Git repository unrelated to ORBIT, initializes and validates `.orbit` configuration, runs diagnostics, checks that a second `init` refuses to overwrite, then runs the deterministic stub-worker end-to-end test. The temporary repository is retained and its path is printed.

The demo makes no GitHub API calls and uses no credentials, paid model, or external agent. It demonstrates local bootstrap and stubbed worker behavior only. It does not prove live ticket admission, production isolation, remote status reporting, or PR delivery. Those claims require the clean-room pilot in issue #7.
