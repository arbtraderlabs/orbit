import json
import copy
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orbit import __main__ as orbit


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "repository"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(["git", "-C", str(self.target), "-c", "user.name=Orbit Test", "-c", "user.email=orbit@example.invalid", "commit", "--allow-empty", "-qm", "baseline"], check=True)
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin", "https://github.com/example/project.git"], check=True)
        self.source_sha = subprocess.run(["git", "-C", str(self.target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        orbit_dir = self.target / ".orbit"
        orbit_dir.mkdir()
        config = json.loads(json.dumps(orbit.DEFAULT))
        config["repository"] = "example/project"
        (orbit_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.policy = self.root / "operator-policy.json"
        self.policy_entry = {
            "ticket_creators": ["creator"],
            "admission": {
                "approvers": ["approver"],
                "stop": False,
                "limits": {
                    "admissions_per_day": 5,
                    "worker_attempts_per_day": 5,
                    "max_concurrent": 3,
                    "max_seconds_per_attempt": 600,
                    "max_retries": 2,
                    "max_request_lifetime_seconds": 3600,
                    "max_ticket_age_seconds": 86400,
                },
            },
            "parent_issue": 4,
        }
        self.write_policy()
        self.task_file = self.root / "task.json"
        self.task_file.write_text(json.dumps({
            "schema_version": 1,
            "title": "Implement a bounded feature",
            "objective": "Implement a feature with a clear scope.",
            "scope": {"allowed_paths": ["src/orbit/"], "prohibited_changes": ["No deployment"]},
            "acceptance_tests": ["The unit suite passes."],
            "dependencies": "None",
            "escalation_boundaries": "Ask before adding credentials.",
        }), encoding="utf-8")
        self.request = self.create_request()
        self.request_file = self.root / "request.json"
        self.request_file.write_text(json.dumps(self.request), encoding="utf-8")
        self.ledger = self.root / "state" / "orbit" / "admissions.sqlite3"
        self.args = SimpleNamespace(
            target=str(self.target), request=str(self.request_file), policy=str(self.policy),
            ledger=str(self.ledger), expires_seconds=600,
            confirm_request_sha256=orbit.canonical_hash(self.request),
        )

    def write_policy(self):
        self.policy.write_text(json.dumps({"schema_version": 1, "repositories": {"example/project": self.policy_entry}}), encoding="utf-8")
        self.policy.chmod(0o600)

    def create_request(self):
        created_payload = {}

        def api(route, method=None, payload=None):
            if route == "user":
                return {"login": "creator", "id": 11}
            if route == "repos/example/project":
                return {"full_name": "example/project", "id": 123}
            if route == "repos/example/project/issues/4":
                return {"id": 44, "number": 4}
            if route == "repos/example/project/issues" and method == "POST":
                created_payload.update(payload)
                return {"id": 55, "number": 12}
            if route == "repos/example/project/issues/12":
                return self.issue(created_payload["body"])
            if route == "repos/example/project/issues/12/parent":
                return {"id": 44, "number": 4}
            raise AssertionError(f"unexpected GitHub call: {method} {route}")

        args = SimpleNamespace(target=str(self.target), request=str(self.task_file), policy=str(self.policy))
        with patch.object(orbit, "gh_api", side_effect=api):
            return orbit.run_ticket_create(args)

    def issue(self, body):
        return {
            "id": 55,
            "number": 12,
            "html_url": "https://github.com/example/project/issues/12",
            "state": "open",
            "user": {"login": "creator", "id": 11},
            "body": body,
            "created_at": self.created_at,
        }

    def live_api(self, route, method=None, payload=None):
        if route == "user":
            return {"login": "approver", "id": 22}
        if route == "repos/example/project":
            return {"full_name": "example/project", "id": 123}
        if route == f"repos/example/project/commits/{self.source_sha}":
            return {"sha": self.source_sha}
        if route == "repos/example/project/issues/12":
            return self.issue(self.created_issue_body)
        if route == "repos/example/project/issues/12/parent":
            return {"id": 44, "number": 4}
        raise AssertionError(f"unexpected GitHub call: {method} {route}")

    def admit(self, api=None):
        self.created_issue_body = self.issue_body()
        with patch.object(orbit, "gh_api", side_effect=api or self.live_api):
            return orbit.admit_request(self.args)

    def issue_body(self):
        rendered = orbit.render_ticket(self.request["ticket"])
        marker = f"<!-- orbit-request:v1 id={self.request['request_id']} source_sha={self.source_sha} policy_sha256={self.request['authorization']['policy_sha256']} -->"
        return rendered["body"] + f"\n\n{marker}\n"

    def test_authorized_request_is_admitted_and_durably_audited(self):
        receipt = self.admit()
        self.assertEqual(receipt["state"], "ADMITTED")
        self.assertEqual(receipt["scope"]["allowed_paths"], ["src/orbit/"])
        self.assertEqual(receipt["ticket"]["title"], "Implement a bounded feature")
        self.assertEqual(receipt["admitted_by"], {"login": "approver", "id": 22})
        self.assertGreater(receipt["expires_at"], receipt["admitted_at"])
        self.assertTrue(self.ledger.is_file())
        connection = sqlite3.connect(self.ledger)
        row = connection.execute("SELECT request_id, request_sha256, admitted_by FROM admissions").fetchone()
        connection.close()
        self.assertEqual(row, (self.request["request_id"], orbit.canonical_hash(self.request), "approver"))

    def test_request_replay_is_rejected(self):
        self.admit()
        with self.assertRaisesRegex(ValueError, "already been admitted"):
            self.admit()

    def test_daily_admission_budget_is_enforced(self):
        first = self.admit()
        second = copy.deepcopy(first)
        second.update(request_id=str(uuid.uuid4()), request_sha256="b" * 64, receipt_id=str(uuid.uuid4()))
        second["issue"].update(id=56, number=13)
        limits = {"admissions_per_day": 1, "max_concurrent": 3}
        with self.assertRaisesRegex(ValueError, "daily admission budget"):
            orbit.record_admission(second, self.ledger, limits)

    def test_concurrency_limit_is_enforced(self):
        first = self.admit()
        second = copy.deepcopy(first)
        second.update(request_id=str(uuid.uuid4()), request_sha256="c" * 64, receipt_id=str(uuid.uuid4()))
        second["issue"].update(id=57, number=14)
        limits = {"admissions_per_day": 5, "max_concurrent": 1}
        with self.assertRaisesRegex(ValueError, "concurrent admission limit"):
            orbit.record_admission(second, self.ledger, limits)

    def test_unapproved_operator_is_rejected(self):
        self.created_issue_body = self.issue_body()

        def unknown_operator(route, method=None, payload=None):
            if route == "user":
                return {"login": "intruder", "id": 99}
            return self.live_api(route, method, payload)

        with patch.object(orbit, "gh_api", side_effect=unknown_operator):
            with self.assertRaisesRegex(ValueError, "not in admission.approvers"):
                orbit.admit_request(self.args)
        self.assertFalse(self.ledger.exists())

    def test_unapproved_ticket_creator_is_rejected(self):
        self.request["requested_by"]["login"] = "intruder"
        self.request_file.write_text(json.dumps(self.request), encoding="utf-8")
        self.args.confirm_request_sha256 = orbit.canonical_hash(self.request)
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "ticket creator is not authorized"):
                orbit.admit_request(self.args)
        api.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_changed_live_issue_is_rejected(self):
        self.created_issue_body = self.issue_body()

        def changed_issue(route, method=None, payload=None):
            result = self.live_api(route, method, payload)
            if route.endswith("/issues/12"):
                result["body"] += " edited"
            return result

        with patch.object(orbit, "gh_api", side_effect=changed_issue):
            with self.assertRaisesRegex(ValueError, "failed identity"):
                orbit.admit_request(self.args)
        self.assertFalse(self.ledger.exists())

    def test_stale_policy_is_rejected(self):
        self.policy_entry["admission"]["limits"]["admissions_per_day"] = 4
        self.write_policy()
        with self.assertRaisesRegex(ValueError, "policy hash is stale"):
            self.admit()
        self.assertFalse(self.ledger.exists())

    def test_stale_source_checkout_is_rejected(self):
        subprocess.run(["git", "-C", str(self.target), "-c", "user.name=Orbit Test", "-c", "user.email=orbit@example.invalid", "commit", "--allow-empty", "-qm", "advance"], check=True)
        with self.assertRaisesRegex(ValueError, "source SHA is stale"):
            self.admit()
        self.assertFalse(self.ledger.exists())

    def test_old_github_ticket_is_rejected(self):
        old = "2020-01-01T00:00:00Z"
        self.request["created_at"] = old
        self.request_file.write_text(json.dumps(self.request), encoding="utf-8")
        self.args.confirm_request_sha256 = orbit.canonical_hash(self.request)
        self.created_issue_body = self.issue_body()

        def stale_ticket(route, method=None, payload=None):
            result = self.live_api(route, method, payload)
            if route.endswith("/issues/12"):
                result["created_at"] = old
            return result

        with patch.object(orbit, "gh_api", side_effect=stale_ticket):
            with self.assertRaisesRegex(ValueError, "stale"):
                orbit.admit_request(self.args)
        self.assertFalse(self.ledger.exists())

    def test_stop_policy_fails_closed(self):
        self.policy_entry["admission"]["stop"] = True
        self.write_policy()
        policy, _, _, _ = orbit.load_policy(self.policy, "example/project")
        with self.assertRaisesRegex(ValueError, "stopped"):
            orbit.admission_settings(policy, "example/project")
        self.assertFalse(self.ledger.exists())

    def test_expiry_cannot_exceed_policy(self):
        self.args.expires_seconds = 3601
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "outside the operator policy limit"):
                orbit.admit_request(self.args)
        api.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_operator_must_confirm_canonical_request_hash(self):
        self.args.confirm_request_sha256 = "wrong"
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "review request details"):
                orbit.admit_request(self.args)
        api.assert_not_called()
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
