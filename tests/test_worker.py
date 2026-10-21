import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orbit import ledger, worker


class WorkerTests(unittest.TestCase):
    def test_command_manifest_requires_private_absolute_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "command.json"
            manifest.write_text(json.dumps(["python3", "-c", "pass"]))
            manifest.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "absolute executable path"):
                worker.load_argv(manifest)
            manifest.write_text(json.dumps([sys.executable, "-c", "pass"]))
            manifest.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                worker.load_argv(manifest)

    def test_scope_enforcement_rejects_escape_and_out_of_scope(self):
        self.assertEqual(worker._within_scope(["src/module.py"], ["src/"]), [])
        self.assertEqual(worker._within_scope(["docs/secret.md"], ["src/"]), ["docs/secret.md"])
        with self.assertRaisesRegex(ValueError, "unsafe path pattern"):
            worker._within_scope([], ["../outside"])

    def test_command_deadline_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            output = root / "output"
            home.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            policy_path = root / "policy.json"
            policy = {"schema_version": 1, "repositories": {"example/project": {"admission": {"stop": False}}}}
            policy_path.write_text(json.dumps(policy))
            policy_path.chmod(0o600)
            policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
            result = worker._run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"], root, home, output, "timeout",
                time.monotonic() + 0.1, policy_path, "example/project", policy_hash, lambda: None,
            )
            self.assertEqual(result["stopped_reason"], "timeout")
            self.assertNotEqual(result["returncode"], 0)

    def test_policy_stop_terminates_running_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            output = root / "output"
            home.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            policy_path = root / "policy.json"
            policy = {"schema_version": 1, "repositories": {"example/project": {"admission": {"stop": False}}}}
            policy_path.write_text(json.dumps(policy))
            policy_path.chmod(0o600)
            policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
            script = (
                "import json,time; from pathlib import Path; p=Path(" + repr(str(policy_path)) + "); "
                "time.sleep(.1); d=json.loads(p.read_text()); d['repositories']['example/project']['admission']['stop']=True; "
                "p.write_text(json.dumps(d)); time.sleep(5)"
            )
            result = worker._run_command(
                [sys.executable, "-c", script], root, home, output, "stopped",
                time.monotonic() + 5, policy_path, "example/project", policy_hash, lambda: None,
            )
            self.assertEqual(result["stopped_reason"], "operator STOP or policy change")
            self.assertNotEqual(result["returncode"], 0)

    def test_stub_worker_e2e_preserves_target_and_existing_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.email", "test@example.invalid"], check=True)
            (target / ".orbit").mkdir()
            config = {
                "schema_version": 1, "repository": "example/project",
                "agent": {"builder": "stub", "reviewer": "human"},
                "limits": {"admissions_per_day": 30, "worker_attempts_per_day": 30, "max_concurrent": 1, "max_seconds_per_attempt": 20, "max_retries": 1},
                "safety": {"stop": True, "auto_merge": False, "allow_deploy": False, "allow_destructive_cleanup": False},
                "delivery": {"pull_request": "draft"},
            }
            (target / ".orbit" / "config.json").write_text(json.dumps(config))
            (target / "README.md").write_text("pinned source\n")
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-qm", "source"], check=True)
            subprocess.run(["git", "-C", str(target), "remote", "add", "origin", "https://github.com/example/project.git"], check=True)
            source_sha = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()

            existing = root / "preexisting-worktree"
            subprocess.run(["git", "-C", str(target), "worktree", "add", "-q", "--detach", str(existing), source_sha], check=True)
            dirty_file = target / "keep-me.txt"
            dirty_file.write_text("untracked user work\n")

            policy_path = root / "policy.json"
            policy = {"schema_version": 1, "repositories": {"example/project": {
                "ticket_creators": ["operator"],
                "admission": {"approvers": ["operator"], "stop": False, "limits": {
                    "admissions_per_day": 10, "worker_attempts_per_day": 10, "max_concurrent": 1,
                    "max_seconds_per_attempt": 20, "max_retries": 1,
                    "max_request_lifetime_seconds": 600, "max_ticket_age_seconds": 600,
                }},
            }}}
            policy_bytes = (json.dumps(policy, sort_keys=True) + "\n").encode()
            policy_path.write_bytes(policy_bytes)
            policy_path.chmod(0o600)
            policy_hash = hashlib.sha256(policy_bytes).hexdigest()

            limits = {"worker_attempts_per_day": 10, "max_concurrent": 1, "max_seconds_per_attempt": 20, "max_retries": 1}
            now = datetime.now(timezone.utc)
            request_id = "stub-request"
            receipt = {
                "request_id": request_id, "receipt_id": "receipt", "state": "ADMITTED",
                "repository": {"id": 77, "full_name": "example/project"},
                "issue": {"id": 99, "number": 9}, "source": {"sha": source_sha},
                "request_sha256": "f" * 64, "admitted_by": {"login": "operator", "id": 1},
                "scope": {"allowed_paths": ["src/"], "prohibited_changes": ["credentials"]},
                "policy_sha256": policy_hash, "policy": {"admission_limits": limits},
                "admitted_at": ledger.iso(now), "expires_at": ledger.iso(now + timedelta(minutes=5)),
            }
            database = root / "ledger.sqlite3"
            connection = sqlite3.connect(database, isolation_level=None)
            ledger.initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, 77, 99, 9, "a" * 64, policy_hash, "operator", receipt["admitted_at"], receipt["expires_at"], json.dumps(receipt, sort_keys=True)),
            )
            ledger.enqueue_admitted(connection, receipt)
            connection.commit()
            claim = ledger.claim_next(connection, 77, "stub-worker", "stub-claim", policy_hash, limits, now=now, lease_seconds=20)

            command = [sys.executable, "-c", "from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/result.txt').write_text('built\\n')"]
            tests = [sys.executable, "-c", "import os; from pathlib import Path; assert Path('src/result.txt').read_text() == 'built\\n'; assert 'SECRET_TOKEN' not in os.environ"]
            os.environ["SECRET_TOKEN"] = "must-not-inherit"
            try:
                result = worker.run_claimed_job(target, 77, "example/project", policy_path, policy_hash, limits, connection, claim, command, tests, root / "state")
            finally:
                os.environ.pop("SECRET_TOKEN", None)
                connection.close()

            self.assertEqual(result["ledger_result"]["state"], "DONE")
            record = result["worker_receipt"]
            self.assertEqual(record["outcome"], "SUCCEEDED")
            self.assertEqual(record["source_sha"], source_sha)
            self.assertEqual(record["changed_paths"], ["src/result.txt"])
            self.assertEqual(record["out_of_scope_paths"], [])
            self.assertIn(str(existing), record["preexisting_worktrees_preserved"])
            self.assertEqual(dirty_file.read_text(), "untracked user work\n")
            self.assertFalse((target / "src" / "result.txt").exists())
            self.assertTrue(Path(record["worktree"], "src", "result.txt").is_file())


if __name__ == "__main__":
    unittest.main()
