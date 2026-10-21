import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orbit import ledger


class QueueLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orbit.sqlite3"
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        ledger.initialize(self.connection)
        self.now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.limits = {
            "worker_attempts_per_day": 30,
            "max_concurrent": 1,
            "max_seconds_per_attempt": 600,
            "max_retries": 2,
        }
        self.receipt = self.make_receipt()
        self.add_admitted_job(self.receipt)

    def tearDown(self):
        self.connection.close()

    def make_receipt(self, request_id="request-1", issue_id=100, issue_number=10, expires=None):
        return {
            "request_id": request_id,
            "receipt_id": f"receipt-{request_id}",
            "state": "ADMITTED",
            "repository": {"id": 7, "full_name": "example/project"},
            "issue": {"id": issue_id, "number": issue_number},
            "source": {"sha": "a" * 40},
            "policy_sha256": "b" * 64,
            "request_sha256": "c" * 64,
            "admitted_by": {"login": "approver", "id": 2},
            "policy": {"admission_limits": self.limits},
            "admitted_at": ledger.iso(self.now),
            "expires_at": ledger.iso(expires or self.now + timedelta(hours=1)),
        }

    def add_admitted_job(self, receipt):
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt["request_id"], receipt["repository"]["id"], receipt["issue"]["id"], receipt["issue"]["number"], "c" * 64, receipt["policy_sha256"], "approver", receipt["admitted_at"], receipt["expires_at"], json.dumps(receipt, sort_keys=True)),
        )
        ledger.enqueue_admitted(self.connection, receipt)
        self.connection.commit()

    def claim(self, key="claim-1", now=None, limits=None):
        return ledger.claim_next(
            self.connection, 7, "worker-a", key, "b" * 64,
            limits or self.limits, now=now or self.now,
        )

    def test_idempotent_claim_returns_same_lease_without_double_attempt(self):
        first = self.claim()
        duplicate = self.claim()
        self.assertEqual(duplicate["lease_token"], first["lease_token"])
        self.assertEqual(duplicate["attempt"], first["attempt"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "another repository, worker, or policy"):
            ledger.claim_next(self.connection, 7, "worker-b", "claim-1", "b" * 64, self.limits, now=self.now)

    def test_restart_recovers_expired_lease_and_prevents_old_worker_completion(self):
        first = self.claim()
        self.connection.close()
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        ledger.initialize(self.connection)
        restarted_at = self.now + timedelta(seconds=61)
        second = self.claim(key="claim-2", now=restarted_at)
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["lease_token"], first["lease_token"])
        self.assertEqual(self.connection.execute("SELECT outcome FROM attempts WHERE attempt_no = 1").fetchone()[0], "INTERRUPTED")
        with self.assertRaisesRegex(ValueError, "lease is missing, expired, or no longer current"):
            ledger.finish_attempt(self.connection, first["job_id"], first["lease_token"], True, now=restarted_at)

    def test_retries_and_exhaustion_are_bounded(self):
        job = self.claim()
        for attempt_no in (1, 2, 3):
            result = ledger.finish_attempt(self.connection, job["job_id"], job["lease_token"], False, {"reason": "failure"}, self.now + timedelta(seconds=attempt_no), max_retries=2)
            if attempt_no < 3:
                self.assertEqual(result["state"], "QUEUED")
                job = self.claim(key=f"claim-{attempt_no + 1}", now=self.now + timedelta(seconds=attempt_no + 1))
                self.assertEqual(job["attempt"], attempt_no + 1)
            else:
                self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 3)

    def test_daily_attempt_budget_rolls_over_in_utc(self):
        self.receipt["expires_at"] = ledger.iso(self.now + timedelta(days=2))
        self.connection.execute(
            "UPDATE jobs SET receipt_json = ? WHERE request_id = ?",
            (json.dumps(self.receipt, sort_keys=True), self.receipt["request_id"]),
        )
        first = self.claim(limits={**self.limits, "worker_attempts_per_day": 1})
        ledger.finish_attempt(self.connection, first["job_id"], first["lease_token"], False, {}, self.now + timedelta(seconds=1), max_retries=2)
        with self.assertRaisesRegex(ValueError, "daily worker-attempt budget"):
            self.claim(key="same-day", now=self.now + timedelta(seconds=2), limits={**self.limits, "worker_attempts_per_day": 1})
        next_day = self.now + timedelta(days=1)
        second = self.claim(key="next-day", now=next_day, limits={**self.limits, "worker_attempts_per_day": 1})
        self.assertEqual(second["attempt"], 2)

    def test_success_and_failed_validation_are_separate_counters(self):
        ledger.record_validation(self.connection, 7, "bad-request", "forged source")
        job = self.claim()
        ledger.finish_attempt(self.connection, job["job_id"], job["lease_token"], True, {"tests": "pass"}, self.now + timedelta(seconds=1), max_retries=2)
        counts = ledger.status(self.connection, 7)
        self.assertEqual(counts["admissions"], 1)
        self.assertEqual(counts["jobs"], {"DONE": 1})
        self.assertEqual(counts["attempts"], {"SUCCEEDED": 1})
        self.assertEqual(counts["failed_validations"], 1)

    def test_stale_policy_and_expired_admission_are_blocked(self):
        stale = self.make_receipt("stale", 101, 11)
        stale["policy_sha256"] = "d" * 64
        self.add_admitted_job(stale)
        expired = self.make_receipt("expired", 102, 12, expires=self.now - timedelta(seconds=1))
        self.add_admitted_job(expired)
        self.connection.execute("UPDATE jobs SET status = 'DONE' WHERE request_id = 'request-1'")
        self.assertIsNone(self.claim())
        statuses = dict(self.connection.execute("SELECT request_id, status FROM jobs"))
        self.assertEqual(statuses["stale"], "BLOCKED")
        self.assertEqual(statuses["expired"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
