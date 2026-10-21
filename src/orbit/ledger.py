"""Durable admission, queue, lease, and worker-attempt records."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def initialize(connection):
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("""CREATE TABLE IF NOT EXISTS admissions (
        request_id TEXT PRIMARY KEY,
        repository_id INTEGER NOT NULL,
        issue_id INTEGER NOT NULL,
        issue_number INTEGER NOT NULL,
        request_sha256 TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        admitted_by TEXT NOT NULL,
        admitted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        UNIQUE(repository_id, issue_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
        request_id TEXT PRIMARY KEY,
        repository_id INTEGER NOT NULL,
        issue_id INTEGER NOT NULL,
        issue_number INTEGER NOT NULL,
        source_sha TEXT NOT NULL,
        status TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        retries_used INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        attempt_deadline_at TEXT,
        failure_reason TEXT,
        receipt_json TEXT NOT NULL,
        UNIQUE(repository_id, issue_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS attempts (
        attempt_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL,
        claim_key TEXT NOT NULL UNIQUE,
        lease_token TEXT NOT NULL UNIQUE,
        worker_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,
        details_json TEXT,
        UNIQUE(request_id, attempt_no)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS job_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        actor TEXT NOT NULL,
        details_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS validation_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        repository_id INTEGER,
        request_id TEXT,
        occurred_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS reviews (
        request_id TEXT PRIMARY KEY,
        diff_sha256 TEXT NOT NULL,
        review_sha256 TEXT NOT NULL UNIQUE,
        reviewed_by TEXT NOT NULL,
        reviewed_at TEXT NOT NULL,
        receipt_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        request_id TEXT PRIMARY KEY,
        head_ref TEXT NOT NULL,
        base_ref TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        pr_url TEXT NOT NULL,
        marker TEXT NOT NULL,
        delivered_at TEXT NOT NULL
    )""")


def enqueue_admitted(connection, receipt):
    request_id = receipt["request_id"]
    queued_at = receipt["admitted_at"]
    connection.execute(
        "INSERT INTO jobs (request_id, repository_id, issue_id, issue_number, source_sha, status, queued_at, receipt_json) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
        (request_id, receipt["repository"]["id"], receipt["issue"]["id"], receipt["issue"]["number"], receipt["source"]["sha"], queued_at, json.dumps(receipt, sort_keys=True)),
    )
    _event(connection, request_id, queued_at, "PROPOSED", "AUTHORISED", "orbit-approval", {"approved_by": receipt["admitted_by"]["login"], "request_sha256": receipt["request_sha256"]})
    _event(connection, request_id, queued_at, "AUTHORISED", "ADMITTED", receipt["admitted_by"]["login"], {"receipt_id": receipt["receipt_id"]})
    _event(connection, request_id, queued_at, "ADMITTED", "QUEUED", "orbit-admission", {"receipt_id": receipt["receipt_id"]})


def record_validation(connection, repository_id, request_id, reason, occurred_at=None):
    connection.execute(
        "INSERT INTO validation_events (repository_id, request_id, occurred_at, outcome, reason) VALUES (?, ?, ?, 'REJECTED', ?)",
        (repository_id, request_id, iso(occurred_at or utc_now()), reason[:1000]),
    )


def _event(connection, request_id, occurred_at, from_state, to_state, actor, details):
    connection.execute(
        "INSERT INTO job_events (request_id, occurred_at, from_state, to_state, actor, details_json) VALUES (?, ?, ?, ?, ?, ?)",
        (request_id, occurred_at, from_state, to_state, actor, json.dumps(details, sort_keys=True)),
    )


def _recover_expired(connection, now, max_retries, repository_id=None):
    query = "SELECT request_id, lease_token, retries_used FROM jobs WHERE status = 'RUNNING' AND lease_expires_at <= ?"
    values = [now]
    if repository_id is not None:
        query += " AND repository_id = ?"
        values.append(repository_id)
    rows = connection.execute(query, values).fetchall()
    for request_id, lease_token, retries_used in rows:
        connection.execute(
            "UPDATE attempts SET finished_at = ?, outcome = 'INTERRUPTED', details_json = ? WHERE request_id = ? AND lease_token = ? AND finished_at IS NULL",
            (now, json.dumps({"reason": "lease expired; recovered after restart"}), request_id, lease_token),
        )
        if retries_used < max_retries:
            new_retries = retries_used + 1
            next_state = "QUEUED"
            reason = "lease expired; retry queued"
        else:
            new_retries = retries_used
            next_state = "BLOCKED"
            reason = "lease expired; retry limit exhausted"
        connection.execute(
            "UPDATE jobs SET status = ?, retries_used = ?, lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL, attempt_deadline_at = NULL, failure_reason = ? WHERE request_id = ?",
            (next_state, new_retries, reason, request_id),
        )
        _event(connection, request_id, now, "RUNNING", next_state, "lease-recovery", {"reason": reason})
    return len(rows)


def _job_result(row, attempt):
    return {
        "job_id": row[0],
        "status": row[5],
        "request": json.loads(row[14]),
        "attempt": attempt[0],
        "worker_id": attempt[1],
        "claim_key": attempt[2],
        "lease_token": row[9],
        "lease_expires_at": row[11],
        "attempt_deadline_at": row[12],
    }


def claim_next(connection, repository_id, worker_id, claim_key, policy_sha256, limits, stop=False, now=None, lease_seconds=60):
    if stop:
        raise ValueError("worker dispatch is stopped by operator policy")
    if not worker_id or not claim_key:
        raise ValueError("worker_id and stable claim_key are required")
    now_dt = now or utc_now()
    now_text = iso(now_dt)
    today = now_text[:10]
    if type(lease_seconds) is not int or lease_seconds < 1 or lease_seconds > limits["max_seconds_per_attempt"]:
        raise ValueError("lease_seconds must be within the per-attempt time limit")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _recover_expired(connection, now_text, limits["max_retries"], repository_id)
        previous = connection.execute(
            "SELECT jobs.request_id, jobs.status, jobs.lease_token, jobs.lease_expires_at, jobs.attempt_deadline_at, attempts.attempt_no, attempts.worker_id, attempts.claim_key, jobs.receipt_json, jobs.repository_id FROM attempts JOIN jobs USING(request_id) WHERE attempts.claim_key = ?",
            (claim_key,),
        ).fetchone()
        if previous:
            if (previous[9] != repository_id or previous[6] != worker_id
                    or json.loads(previous[8]).get("policy_sha256") != policy_sha256):
                raise ValueError("claim_key is already bound to another repository, worker, or policy")
            if previous[1] == "RUNNING" and previous[3] and previous[3] > now_text:
                attempt = (previous[5], previous[6], previous[7])
                connection.commit()
                row = connection.execute("SELECT * FROM jobs WHERE request_id = ?", (previous[0],)).fetchone()
                return _job_result(row, attempt)
            raise ValueError("claim_key has already been consumed")

        active = connection.execute("SELECT COUNT(*) FROM jobs WHERE repository_id = ? AND status = 'RUNNING' AND lease_expires_at > ?", (repository_id, now_text)).fetchone()[0]
        if active >= limits["max_concurrent"]:
            raise ValueError("concurrent worker limit reached")
        attempts_today = connection.execute(
            "SELECT COUNT(*) FROM attempts JOIN jobs USING(request_id) WHERE jobs.repository_id = ? AND substr(attempts.started_at, 1, 10) = ?",
            (repository_id, today),
        ).fetchone()[0]
        if attempts_today >= limits["worker_attempts_per_day"]:
            raise ValueError("daily worker-attempt budget exhausted")

        candidates = connection.execute("SELECT * FROM jobs WHERE repository_id = ? AND status = 'QUEUED' ORDER BY queued_at, request_id", (repository_id,)).fetchall()
        selected = None
        for candidate in candidates:
            receipt = json.loads(candidate[14])
            if receipt.get("policy_sha256") != policy_sha256:
                connection.execute("UPDATE jobs SET status = 'BLOCKED', failure_reason = 'operator policy changed after admission' WHERE request_id = ?", (candidate[0],))
                _event(connection, candidate[0], now_text, "QUEUED", "BLOCKED", "policy-check", {"reason": "operator policy changed after admission"})
                continue
            if receipt.get("expires_at", "") <= now_text:
                connection.execute("UPDATE jobs SET status = 'BLOCKED', failure_reason = 'admission receipt expired' WHERE request_id = ?", (candidate[0],))
                _event(connection, candidate[0], now_text, "QUEUED", "BLOCKED", "expiry-check", {"reason": "admission receipt expired"})
                continue
            selected = candidate
            break
        if selected is None:
            connection.commit()
            return None

        attempt_no = selected[8] + 1
        lease_token = str(uuid.uuid4())
        receipt = json.loads(selected[14])
        receipt_expiry = datetime.fromisoformat(receipt["expires_at"].replace("Z", "+00:00"))
        attempt_deadline = min(now_dt + timedelta(seconds=limits["max_seconds_per_attempt"]), receipt_expiry)
        lease_expiry = min(now_dt + timedelta(seconds=lease_seconds), attempt_deadline)
        lease_expiry_text = iso(lease_expiry)
        deadline_text = iso(attempt_deadline)
        attempt_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO attempts (attempt_id, request_id, attempt_no, claim_key, lease_token, worker_id, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, selected[0], attempt_no, claim_key, lease_token, worker_id, now_text),
        )
        connection.execute(
            "UPDATE jobs SET status = 'RUNNING', attempt_count = ?, lease_token = ?, lease_owner = ?, lease_expires_at = ?, attempt_deadline_at = ?, failure_reason = NULL WHERE request_id = ? AND status = 'QUEUED'",
            (attempt_no, lease_token, worker_id, lease_expiry_text, deadline_text, selected[0]),
        )
        _event(connection, selected[0], now_text, "QUEUED", "RUNNING", worker_id, {"attempt": attempt_no, "claim_key": claim_key, "lease_expires_at": lease_expiry_text})
        connection.commit()
        row = connection.execute("SELECT * FROM jobs WHERE request_id = ?", (selected[0],)).fetchone()
        return _job_result(row, (attempt_no, worker_id, claim_key))
    except Exception:
        connection.rollback()
        raise


def heartbeat(connection, request_id, lease_token, now=None, lease_seconds=60, policy_sha256=None):
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    now_text = iso(now or utc_now())
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT lease_expires_at, attempt_deadline_at, lease_owner, receipt_json FROM jobs WHERE request_id = ? AND status = 'RUNNING' AND lease_token = ?", (request_id, lease_token)).fetchone()
        if not row or row[0] <= now_text or row[1] <= now_text:
            raise ValueError("lease is missing, expired, or no longer current")
        if policy_sha256 is not None and json.loads(row[3]).get("policy_sha256") != policy_sha256:
            raise ValueError("operator policy changed during the active attempt")
        receipt_expiry = json.loads(row[3]).get("expires_at", row[1])
        new_expiry = min(iso((now or utc_now()) + timedelta(seconds=lease_seconds)), row[1], receipt_expiry)
        connection.execute("UPDATE jobs SET lease_expires_at = ? WHERE request_id = ? AND lease_token = ?", (new_expiry, request_id, lease_token))
        connection.commit()
        return {"request_id": request_id, "lease_expires_at": new_expiry, "worker_id": row[2]}
    except Exception:
        connection.rollback()
        raise


def finish_attempt(connection, request_id, lease_token, success, details=None, now=None, max_retries=0, retryable=True):
    now_text = iso(now or utc_now())
    connection.execute("BEGIN IMMEDIATE")
    try:
        job = connection.execute("SELECT status, retries_used, lease_expires_at, attempt_deadline_at, receipt_json FROM jobs WHERE request_id = ? AND lease_token = ?", (request_id, lease_token)).fetchone()
        if not job or job[0] != "RUNNING" or job[2] <= now_text or job[3] <= now_text:
            raise ValueError("lease is missing, expired, or no longer current")
        if json.loads(job[4]).get("expires_at", "") <= now_text:
            raise ValueError("admission receipt expired during the worker attempt")
        if max_retries is None:
            max_retries = json.loads(job[4])["policy"]["admission_limits"]["max_retries"]
        if success:
            state = "DONE"
            retries_used = job[1]
            outcome = "SUCCEEDED"
        elif retryable and job[1] < max_retries:
            state = "QUEUED"
            retries_used = job[1] + 1
            outcome = "FAILED_RETRYABLE"
        else:
            state = "BLOCKED"
            retries_used = job[1]
            outcome = "FAILED_EXHAUSTED" if retryable else "FAILED_BLOCKED"
        connection.execute("UPDATE attempts SET finished_at = ?, outcome = ?, details_json = ? WHERE request_id = ? AND lease_token = ? AND finished_at IS NULL", (now_text, outcome, json.dumps(details or {}, sort_keys=True), request_id, lease_token))
        connection.execute("UPDATE jobs SET status = ?, retries_used = ?, lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL, attempt_deadline_at = NULL, failure_reason = ? WHERE request_id = ?", (state, retries_used, None if success else json.dumps(details or {}, sort_keys=True), request_id))
        _event(connection, request_id, now_text, "RUNNING", state, "worker-result", {"outcome": outcome, "details": details or {}})
        connection.commit()
        return {"request_id": request_id, "state": state, "outcome": outcome, "retries_used": retries_used}
    except Exception:
        connection.rollback()
        raise


def status(connection, repository_id):
    jobs = dict(connection.execute("SELECT status, COUNT(*) FROM jobs WHERE repository_id = ? GROUP BY status", (repository_id,)).fetchall())
    attempts = dict(connection.execute("SELECT attempts.outcome, COUNT(*) FROM attempts JOIN jobs USING(request_id) WHERE jobs.repository_id = ? GROUP BY attempts.outcome", (repository_id,)).fetchall())
    validations = connection.execute("SELECT COUNT(*) FROM validation_events WHERE repository_id = ? AND outcome = 'REJECTED'", (repository_id,)).fetchone()[0]
    admissions = connection.execute("SELECT COUNT(*) FROM admissions WHERE repository_id = ?", (repository_id,)).fetchone()[0]
    return {"admissions": admissions, "jobs": jobs, "attempts": attempts, "failed_validations": validations}
