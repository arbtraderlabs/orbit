import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orbit import __main__ as orbit


class TicketGeneratorTests(unittest.TestCase):
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
        self.policy = self.root / "operator-policy.json"
        self.policy.write_text(json.dumps({"schema_version": 1, "repositories": {"example/project": {"ticket_creators": ["operator"], "parent_issue": 4}}}), encoding="utf-8")
        self.policy.chmod(0o600)
        self.ticket = self.root / "ticket.json"
        self.ticket.write_text(json.dumps({
            "schema_version": 1,
            "title": "Implement a bounded task",
            "objective": "Create a small scoped feature.",
            "scope": {"allowed_paths": ["src/orbit"], "prohibited_changes": ["No deployment"]},
            "acceptance_tests": ["Unit tests pass"],
            "dependencies": "None",
            "escalation_boundaries": "Ask before adding credentials.",
        }), encoding="utf-8")
        self.args = SimpleNamespace(target=str(self.target), request=str(self.ticket), policy=str(self.policy))
        self.created_payload = None

    def fake_github(self, route, method=None, payload=None):
        if route == "user":
            return {"login": "operator", "id": 91}
        if route == "repos/example/project":
            return {"full_name": "Example/Project", "id": 123}
        if route == "repos/example/project/issues/4":
            return {"id": 444, "number": 4, "html_url": "https://github.com/example/project/issues/4"}
        if route == "repos/example/project/issues" and method == "POST":
            self.created_payload = payload
            return {"id": 555, "number": 12}
        if route == "repos/example/project/issues/12":
            body = self.created_payload["body"]
            return {"id": 555, "number": 12, "html_url": "https://github.com/example/project/issues/12", "user": {"login": "operator", "id": 91}, "body": body, "created_at": "2026-10-21T00:00:00Z"}
        if route == "repos/example/project/issues/12/parent":
            return {"id": 444, "number": 4}
        raise AssertionError(f"unexpected GitHub API call: {method} {route}")

    def test_creates_pinned_proposed_request_and_verifies_parent(self):
        with patch.object(orbit, "gh_api", side_effect=self.fake_github) as api:
            request = orbit.run_ticket_create(self.args)

        self.assertEqual(request["state"], "PROPOSED")
        self.assertEqual(request["source"]["sha"], self.source_sha)
        self.assertEqual(request["ticket"]["scope"]["allowed_paths"], ["src/orbit"])
        self.assertEqual(request["repository"]["id"], 123)
        self.assertEqual(request["issue"]["number"], 12)
        self.assertEqual(request["issue"]["parent_issue"], 4)
        self.assertEqual(request["requested_by"], {"login": "operator", "id": 91})
        self.assertEqual(request["authorization"]["job_admission"], "not_authorized")
        self.assertEqual(len(request["authorization"]["policy_sha256"]), 64)
        self.assertEqual(self.created_payload["parent_issue_id"], 444)
        self.assertIn(self.source_sha, self.created_payload["body"])
        self.assertIn(request["authorization"]["policy_sha256"], self.created_payload["body"])
        self.assertIn("## Allowed paths", self.created_payload["body"])
        self.assertIn("## Acceptance tests and evidence", self.created_payload["body"])
        self.assertEqual(api.call_count, 6)

    def test_unlisted_authenticated_actor_cannot_create_issue(self):
        with patch.object(orbit, "gh_api", return_value={"login": "intruder", "id": 92}) as api:
            with self.assertRaisesRegex(ValueError, "not in the operator ticket-creator allowlist"):
                orbit.run_ticket_create(self.args)
        self.assertEqual(api.call_count, 1)

    def test_git_remote_must_match_configuration(self):
        subprocess.run(["git", "-C", str(self.target), "remote", "set-url", "origin", "https://github.com/elsewhere/project.git"], check=True)
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "origin does not match"):
                orbit.run_ticket_create(self.args)
        api.assert_not_called()

    def test_operator_policy_cannot_live_in_target_checkout(self):
        policy = self.target / "operator-policy.json"
        policy.write_text(self.policy.read_text(encoding="utf-8"), encoding="utf-8")
        policy.chmod(0o600)
        self.args.policy = str(policy)
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "outside the target repository"):
                orbit.run_ticket_create(self.args)
        api.assert_not_called()

    def test_ticket_schema_rejects_missing_scope_before_api_call(self):
        self.ticket.write_text(json.dumps({"schema_version": 1, "title": "Bad ticket"}), encoding="utf-8")
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "fields do not match"):
                orbit.run_ticket_create(self.args)
        api.assert_not_called()

    def test_symlinked_orbit_directory_is_rejected(self):
        external = self.root / "external-orbit"
        (self.target / ".orbit").rename(external)
        (self.target / ".orbit").symlink_to(external, target_is_directory=True)
        with patch.object(orbit, "gh_api") as api:
            with self.assertRaisesRegex(ValueError, "missing safe"):
                orbit.run_ticket_create(self.args)
        api.assert_not_called()

    def test_changed_issue_body_fails_verification(self):
        def changed_body(route, method=None, payload=None):
            result = self.fake_github(route, method, payload)
            if route.endswith("/issues/12"):
                result["body"] += " edited"
            return result

        with patch.object(orbit, "gh_api", side_effect=changed_body):
            with self.assertRaisesRegex(ValueError, "body failed verification"):
                orbit.run_ticket_create(self.args)

    def test_issue_must_be_created_by_verified_actor(self):
        def forged_creator(route, method=None, payload=None):
            result = self.fake_github(route, method, payload)
            if route.endswith("/issues/12"):
                result["user"]["login"] = "someone-else"
            return result

        with patch.object(orbit, "gh_api", side_effect=forged_creator):
            with self.assertRaisesRegex(ValueError, "creator"):
                orbit.run_ticket_create(self.args)

    def test_parent_relationship_must_match_configuration(self):
        def wrong_parent(route, method=None, payload=None):
            result = self.fake_github(route, method, payload)
            if route.endswith("/issues/12/parent"):
                return {"id": 999, "number": 8}
            return result

        with patch.object(orbit, "gh_api", side_effect=wrong_parent):
            with self.assertRaisesRegex(ValueError, "configured parent relationship"):
                orbit.run_ticket_create(self.args)

    def test_rejects_group_writable_operator_policy(self):
        self.policy.chmod(0o620)
        with self.assertRaisesRegex(ValueError, "must not be group or world writable"):
            orbit.load_policy(self.policy, "example/project")

    def test_request_output_refuses_overwrite(self):
        output = self.root / "request.json"
        output.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            orbit.write_request(output, {"state": "PROPOSED"})
        self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_request_output_is_machine_readable(self):
        output = self.root / "request.json"
        expected = {"state": "PROPOSED", "source": {"sha": self.source_sha}}
        orbit.write_request(output, expected)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)

    def test_github_remote_formats(self):
        for remote in ("https://github.com/example/project.git", "git@github.com:example/project.git", "ssh://git@github.com/example/project.git"):
            self.assertEqual(orbit.github_remote_repository(remote), "example/project")


if __name__ == "__main__":
    unittest.main()
