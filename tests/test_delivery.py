import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orbit import delivery, ledger


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.connection = sqlite3.connect(self.root / "ledger.sqlite3", isolation_level=None)
        ledger.initialize(self.connection)
        now = datetime.now(timezone.utc)
        self.request_id = "delivery-request"
        self.admission = {
            "request_id": self.request_id,
            "receipt_id": "admission-receipt",
            "repository": {"id": 8, "full_name": "example/project"},
            "issue": {"id": 80, "number": 8, "url": "https://github.com/example/project/issues/8"},
            "requested_by": {"login": "requester", "id": 1},
            "admitted_by": {"login": "approver", "id": 2},
            "source": {"sha": "a" * 40},
            "ticket": {"title": "Deliver tested change"},
            "scope": {"allowed_paths": ["src/"]},
            "policy_sha256": "b" * 64,
            "admitted_at": ledger.iso(now),
            "expires_at": ledger.iso(now + timedelta(hours=1)),
        }
        self.changed_files = {"src/result.txt": {"status": "present", "blob_sha": "c" * 40}}
        self.worker_record = {
            "schema_version": 1,
            "request_id": self.request_id,
            "source_sha": "a" * 40,
            "outcome": "SUCCEEDED",
            "diff_sha256": "d" * 64,
            "changed_paths": ["src/result.txt"],
            "changed_files": self.changed_files,
            "out_of_scope_paths": [],
        }
        self.worker_path = self.root / "worker-receipt.json"
        self.worker_bytes = (json.dumps(self.worker_record, sort_keys=True, indent=2) + "\n").encode()
        self.worker_path.write_bytes(self.worker_bytes)
        self.worker_path.chmod(0o600)
        self.worker_hash = hashlib.sha256(self.worker_bytes).hexdigest()
        self.connection.execute(
            "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.request_id, 8, 80, 8, "e" * 64, "b" * 64, "approver", self.admission["admitted_at"], self.admission["expires_at"], json.dumps(self.admission, sort_keys=True)),
        )
        self.connection.execute(
            "INSERT INTO jobs (request_id, repository_id, issue_id, issue_number, source_sha, status, queued_at, receipt_json) VALUES (?, 8, 80, 8, ?, 'DONE', ?, ?)",
            (self.request_id, "a" * 40, self.admission["admitted_at"], json.dumps(self.admission, sort_keys=True)),
        )
        self.connection.execute(
            "INSERT INTO attempts (attempt_id, request_id, attempt_no, claim_key, lease_token, worker_id, started_at, finished_at, outcome, details_json) VALUES ('attempt', ?, 1, 'claim', 'lease', 'worker', ?, ?, 'SUCCEEDED', ?)",
            (self.request_id, self.admission["admitted_at"], self.admission["admitted_at"], json.dumps({"worker_receipt": str(self.worker_path), "worker_receipt_sha256": self.worker_hash})),
        )

    def tearDown(self):
        self.connection.close()

    def test_worker_completion_does_not_create_acceptance_or_allow_pr(self):
        snapshot = delivery.status_snapshot(self.connection, self.request_id)
        self.assertEqual(snapshot["state"], "DONE")
        self.assertIn("ACCEPTED", snapshot["stages"])
        with self.assertRaisesRegex(ValueError, "requires an independent ACCEPTED"):
            delivery.create_draft_pr(lambda *a, **k: self.fail("GitHub must not be called"), self.connection, self.request_id, "feature", "main", "x", "reviewer")

    def test_review_requires_exact_diff_and_distinct_reviewer(self):
        with self.assertRaisesRegex(ValueError, "differ from the ticket creator"):
            delivery.accept_review(self.connection, self.request_id, "approver", 2, "reviewed", "d" * 64)
        with self.assertRaisesRegex(ValueError, "repeat with --confirm-diff-sha256"):
            delivery.accept_review(self.connection, self.request_id, "reviewer", 3, "reviewed", "0" * 64)
        review, review_hash = delivery.accept_review(self.connection, self.request_id, "reviewer", 3, "reviewed", "d" * 64)
        same_review, same_hash = delivery.accept_review(self.connection, self.request_id, "reviewer", 3, "reviewed", "d" * 64)
        self.assertEqual(review["outcome"], "ACCEPTED")
        self.assertEqual(same_review, review)
        self.assertEqual(same_hash, review_hash)
        self.assertEqual(delivery.status_snapshot(self.connection, self.request_id)["state"], "ACCEPTED")

    def test_draft_pr_reconciles_api_failure_and_is_idempotent(self):
        _, review_hash = delivery.accept_review(self.connection, self.request_id, "reviewer", 3, "reviewed", "d" * 64)
        created = []

        def api(route, method=None, payload=None):
            if "/compare/main..." in route:
                return {"status": "ahead", "base_commit": {"sha": "base-sha"}, "merge_base_commit": {"sha": "base-sha"}}
            if "/compare/" in route:
                return {
                    "status": "ahead",
                    "base_commit": {"sha": "a" * 40},
                    "head_commit": {"sha": "head-sha"},
                    "files": [{"filename": "src/result.txt", "status": "added", "sha": "c" * 40}],
                }
            if "/pulls?" in route:
                return list(created)
            if route.endswith("/pulls") and method == "POST":
                pull = {
                    "number": 17, "html_url": "https://github.com/example/project/pull/17",
                    "body": payload["body"], "draft": True,
                    "head": {"ref": "feature", "sha": "head-sha"},
                    "base": {"ref": "main", "sha": "base-sha"},
                }
                created.append(pull)
                raise ValueError("simulated lost API response")
            self.fail(f"unexpected GitHub API route: {route}")

        first = delivery.create_draft_pr(api, self.connection, self.request_id, "feature", "main", review_hash, "reviewer")
        second = delivery.create_draft_pr(api, self.connection, self.request_id, "feature", "main", review_hash, "reviewer")
        self.assertEqual(first["number"], 17)
        self.assertTrue(first["draft"])
        self.assertEqual(second, first)
        self.assertEqual(len(created), 1)
        snapshot = delivery.status_snapshot(self.connection, self.request_id)
        self.assertEqual(snapshot["state"], "ACCEPTED")
        self.assertEqual(snapshot["draft_pr"]["number"], 17)

    def test_draft_pr_requires_authenticated_reviewer_identity(self):
        _, review_hash = delivery.accept_review(self.connection, self.request_id, "reviewer", 3, "reviewed", "d" * 64)
        with self.assertRaisesRegex(ValueError, "authenticated reviewer"):
            delivery.create_draft_pr(lambda *a, **k: self.fail("GitHub must not be called"), self.connection, self.request_id, "feature", "main", review_hash, "approver")

    def test_status_comment_post_is_reconciled_without_duplicate(self):
        snapshot = delivery.status_snapshot(self.connection, self.request_id)
        marker, body = delivery.status_comment(snapshot)
        comments = []
        next_id = [1]

        def api(route, method=None, payload=None):
            if "/comments?" in route:
                return list(comments)
            if route.endswith("/comments") and method == "POST":
                comments.append({"id": next_id[0], "body": payload["body"]})
                next_id[0] += 1
                raise ValueError("simulated lost API response")
            self.fail(f"unexpected GitHub API route: {route}")

        first = delivery.publish_status(api, "example/project", 8, marker, body)
        second = delivery.publish_status(api, "example/project", 8, marker, body)
        self.assertEqual(first["comment_id"], 1)
        self.assertEqual(second["comment_id"], 1)
        self.assertEqual(len(comments), 1)

    def test_status_comment_update_is_reconciled_after_api_error(self):
        snapshot = delivery.status_snapshot(self.connection, self.request_id)
        marker, body = delivery.status_comment(snapshot)
        comments = [{"id": 22, "body": marker + "\nold status"}]

        def api(route, method=None, payload=None):
            if "/comments?" in route:
                return list(comments)
            if route.endswith("/comments/22") and method == "PATCH":
                comments[0]["body"] = payload["body"]
                raise ValueError("simulated lost update response")
            self.fail(f"unexpected GitHub API route: {route}")

        result = delivery.publish_status(api, "example/project", 8, marker, body)
        self.assertEqual(result["comment_id"], 22)
        self.assertEqual(comments[0]["body"], body)


if __name__ == "__main__":
    unittest.main()
