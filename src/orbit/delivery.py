"""Independent review receipts, stage reporting, and gated draft PR delivery."""

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode


def canonical_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _private_json(path_value):
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("worker receipt must be a regular file, not a symlink")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or (hasattr(os, "getuid") and metadata.st_uid != os.getuid()) or metadata.st_mode & 0o077:
        raise ValueError("worker receipt must be private and owned by the current user")
    content = path.read_bytes()
    return json.loads(content), hashlib.sha256(content).hexdigest()


def verified_worker_receipt(connection, request_id):
    job = connection.execute("SELECT status, source_sha, receipt_json FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
    if not job or job[0] != "DONE":
        raise ValueError("only a completed DONE job can be reviewed or delivered")
    receipt = json.loads(job[2])
    attempt = connection.execute("SELECT outcome, details_json FROM attempts WHERE request_id = ? ORDER BY attempt_no DESC LIMIT 1", (request_id,)).fetchone()
    if not attempt or attempt[0] != "SUCCEEDED":
        raise ValueError("latest worker attempt did not succeed")
    details = json.loads(attempt[1] or "{}")
    receipt_path = details.get("worker_receipt")
    expected_hash = details.get("worker_receipt_sha256")
    if not isinstance(receipt_path, str) or not isinstance(expected_hash, str):
        raise ValueError("completed attempt has no verifiable worker evidence receipt")
    worker_receipt, actual_hash = _private_json(receipt_path)
    if actual_hash != expected_hash:
        raise ValueError("worker evidence receipt hash does not match the ledger")
    if (worker_receipt.get("request_id") != request_id or worker_receipt.get("source_sha") != job[1]
            or worker_receipt.get("outcome") != "SUCCEEDED" or worker_receipt.get("out_of_scope_paths")
            or not isinstance(worker_receipt.get("diff_sha256"), str)
            or not isinstance(worker_receipt.get("changed_files"), dict)):
        raise ValueError("worker evidence receipt failed source, outcome, or scope verification")
    return receipt, worker_receipt, expected_hash


def accept_review(connection, request_id, reviewer, reviewer_id, notes, confirm_diff_sha256):
    admission, evidence, worker_receipt_sha256 = verified_worker_receipt(connection, request_id)
    if (reviewer.casefold() == admission.get("admitted_by", {}).get("login", "").casefold()
            or reviewer.casefold() == admission.get("requested_by", {}).get("login", "").casefold()):
        raise ValueError("independent reviewer must differ from the ticket creator and admission approver")
    diff_hash = evidence["diff_sha256"]
    if confirm_diff_sha256 != diff_hash:
        raise ValueError(f"review the pinned worker diff, then repeat with --confirm-diff-sha256 {diff_hash}")
    reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    review = {
        "schema_version": 1,
        "request_id": request_id,
        "outcome": "ACCEPTED",
        "diff_sha256": diff_hash,
        "worker_receipt_sha256": worker_receipt_sha256,
        "reviewed_by": {"login": reviewer, "id": reviewer_id},
        "reviewed_at": reviewed_at,
        "notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
    }
    review_hash = canonical_hash(review)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute("SELECT review_sha256, receipt_json FROM reviews WHERE request_id = ?", (request_id,)).fetchone()
        if existing:
            previous = json.loads(existing[1])
            if (previous.get("diff_sha256") == diff_hash
                    and previous.get("reviewed_by") == review["reviewed_by"]
                    and previous.get("notes_sha256") == review["notes_sha256"]):
                connection.commit()
                return previous, existing[0]
            raise ValueError("a different review receipt already exists for this request")
        connection.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, diff_hash, review_hash, reviewer, reviewed_at, json.dumps(review, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO job_events (request_id, occurred_at, from_state, to_state, actor, details_json) VALUES (?, ?, 'DONE', 'ACCEPTED', ?, ?)",
            (request_id, reviewed_at, reviewer, json.dumps({"review_sha256": review_hash, "diff_sha256": diff_hash}, sort_keys=True)),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return review, review_hash


def status_snapshot(connection, request_id):
    job = connection.execute("SELECT status, receipt_json FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
    admission = connection.execute("SELECT receipt_json FROM admissions WHERE request_id = ?", (request_id,)).fetchone()
    if not admission:
        return {"request_id": request_id, "state": "PROPOSED", "stages": ["PROPOSED", "AUTHORISED", "ADMITTED", "RUNNING", "DONE", "ACCEPTED"]}
    events = [
        {"from": row[0], "to": row[1], "at": row[2], "actor": row[3]}
        for row in connection.execute("SELECT from_state, to_state, occurred_at, actor FROM job_events WHERE request_id = ? ORDER BY event_id", (request_id,))
    ]
    job_receipt = json.loads(job[1]) if job else json.loads(admission[0])
    current = job[0] if job else "ADMITTED"
    review = connection.execute("SELECT review_sha256, receipt_json FROM reviews WHERE request_id = ?", (request_id,)).fetchone()
    delivered = connection.execute("SELECT pr_number, pr_url, head_sha FROM deliveries WHERE request_id = ?", (request_id,)).fetchone()
    stage = "ACCEPTED" if review else ("DONE" if current == "DONE" else "RUNNING" if current == "RUNNING" else "ADMITTED" if current in ("QUEUED", "ADMITTED") else current)
    return {
        "request_id": request_id,
        "repository": job_receipt.get("repository", {}).get("full_name"),
        "issue_number": job_receipt.get("issue", {}).get("number"),
        "state": stage,
        "worker_state": current,
        "stages": ["PROPOSED", "AUTHORISED", "ADMITTED", "RUNNING", "DONE", "ACCEPTED"],
        "events": events,
        "review": json.loads(review[1]) if review else None,
        "draft_pr": {"number": delivered[0], "url": delivered[1], "head_sha": delivered[2]} if delivered else None,
    }


def status_comment(snapshot):
    marker = f"<!-- orbit-status:v1 request_id={snapshot['request_id']} -->"
    lines = [marker, "## ORBIT status", f"**Current state:** `{snapshot['state']}`"]
    if snapshot.get("worker_state"):
        lines.append(f"**Worker state:** `{snapshot['worker_state']}`")
    lines.append("**Distinct stages:** " + " → ".join(f"`{stage}`" for stage in snapshot["stages"]))
    if snapshot.get("review"):
        lines.append(f"**Accepted diff SHA-256:** `{snapshot['review']['diff_sha256']}`")
        lines.append(f"**Independent reviewer:** @{snapshot['review']['reviewed_by']['login']}")
    if snapshot.get("draft_pr"):
        lines.append(f"**Draft PR:** [#{snapshot['draft_pr']['number']}]({snapshot['draft_pr']['url']}) at `{snapshot['draft_pr']['head_sha']}`")
    if snapshot.get("events"):
        transitions = [f"`{event['from'] or 'START'}` → `{event['to']}`" for event in snapshot["events"]]
        lines.append("**Recorded transitions:** " + " → ".join(transitions))
    lines.append("This status report is informational; a worker result alone never marks work ACCEPTED.")
    return marker, "\n\n".join(lines)


def _matching_comments(api, repository, issue_number, marker):
    matching = []
    for page in range(1, 101):
        rows = api(f"repos/{repository}/issues/{issue_number}/comments?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("GitHub returned an invalid issue-comment list")
        matching.extend(row for row in rows if isinstance(row, dict) and isinstance(row.get("body"), str) and marker in row["body"])
        if len(rows) < 100:
            break
    return matching


def publish_status(api, repository, issue_number, marker, body):
    comments = _matching_comments(api, repository, issue_number, marker)
    if comments:
        comment = min(comments, key=lambda row: row.get("id", 0))
        if comment.get("body") != body:
            try:
                api(f"repos/{repository}/issues/comments/{comment['id']}", method="PATCH", payload={"body": body})
            except ValueError:
                comments = _matching_comments(api, repository, issue_number, marker)
                if not any(row.get("body") == body for row in comments):
                    raise
    else:
        try:
            api(f"repos/{repository}/issues/{issue_number}/comments", method="POST", payload={"body": body})
        except ValueError:
            comments = _matching_comments(api, repository, issue_number, marker)
            if not comments:
                raise
    comments = _matching_comments(api, repository, issue_number, marker)
    exact = [row for row in comments if row.get("body") == body]
    if not exact:
        raise ValueError("GitHub status comment could not be reconciled after update")
    return {"comment_id": min(exact, key=lambda row: row.get("id", 0))["id"], "idempotent": bool(comments)}


def _api_compare(api, repository, base, head):
    route = f"repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
    value = api(route)
    if not isinstance(value, dict):
        raise ValueError("GitHub returned an invalid compare result")
    return value


def _pulls(api, repository, head_ref, base_ref):
    query = urlencode({"state": "all", "head": head_ref, "base": base_ref, "per_page": 100})
    rows = api(f"repos/{repository}/pulls?{query}")
    if not isinstance(rows, list):
        raise ValueError("GitHub returned an invalid pull-request list")
    return rows


def create_draft_pr(api, connection, request_id, head_ref, base_ref, confirm_review_sha256, authenticated_actor):
    admission, evidence, worker_receipt_sha256 = verified_worker_receipt(connection, request_id)
    review_row = connection.execute("SELECT review_sha256, receipt_json FROM reviews WHERE request_id = ?", (request_id,)).fetchone()
    if not review_row:
        raise ValueError("draft PR delivery requires an independent ACCEPTED review receipt")
    review_hash, review_json = review_row
    if review_hash != confirm_review_sha256:
        raise ValueError(f"verify the ACCEPTED review receipt, then repeat with --confirm-review-sha256 {review_hash}")
    review = json.loads(review_json)
    if (canonical_hash(review) != review_hash or review.get("request_id") != request_id
            or review.get("outcome") != "ACCEPTED" or review.get("diff_sha256") != evidence.get("diff_sha256")
            or review.get("worker_receipt_sha256") != worker_receipt_sha256
            or review.get("reviewed_by", {}).get("login", "").casefold() != str(authenticated_actor).casefold()
            or review.get("reviewed_by", {}).get("login", "").casefold() in {
                admission.get("admitted_by", {}).get("login", "").casefold(),
                admission.get("requested_by", {}).get("login", "").casefold(),
            }):
        raise ValueError("accepted review receipt is invalid, stale, or not held by the authenticated reviewer")
    repository = admission["repository"]["full_name"]
    source_sha = admission["source"]["sha"]
    if not evidence.get("changed_files"):
        raise ValueError("cannot create a draft PR for an empty worker diff")
    base_ref = base_ref.strip()
    head_ref = head_ref.strip()
    if not base_ref or not head_ref or base_ref == head_ref or any(ch.isspace() for ch in base_ref + head_ref):
        raise ValueError("head and base must be distinct GitHub refs")

    source_check = _api_compare(api, repository, base_ref, source_sha)
    source_base_sha = source_check.get("base_commit", {}).get("sha")
    merge_base_sha = source_check.get("merge_base_commit", {}).get("sha")
    if source_check.get("status") not in ("ahead", "identical") or source_base_sha != merge_base_sha:
        raise ValueError("PR base must be an ancestor of the pinned source commit")

    compare = _api_compare(api, repository, source_sha, head_ref)
    head_commit = compare.get("head_commit")
    if (compare.get("status") != "ahead" or compare.get("base_commit", {}).get("sha") != source_sha
            or not isinstance(head_commit, dict) or not isinstance(head_commit.get("sha"), str)):
        raise ValueError("remote head branch must contain changes after the pinned source commit")
    remote_files = compare.get("files")
    if not isinstance(remote_files, list) or len(remote_files) != len(evidence["changed_files"]):
        raise ValueError("remote head file list does not match worker evidence")
    expected_files = evidence["changed_files"]
    if len(expected_files) > 250:
        raise ValueError("remote diff exceeds the supported 250-file verification limit")
    seen = set()
    for remote in remote_files:
        if not isinstance(remote, dict) or not isinstance(remote.get("filename"), str) or remote.get("previous_filename"):
            raise ValueError("remote comparison contains an unsupported or renamed file")
        path = remote["filename"]
        expected = expected_files.get(path)
        if not expected or path in seen:
            raise ValueError("remote head includes a path outside the accepted worker diff")
        seen.add(path)
        if expected["status"] == "removed":
            if remote.get("status") != "removed":
                raise ValueError("remote deletion does not match accepted worker evidence")
        elif expected["status"] in ("present", "symlink"):
            if remote.get("sha") != expected["blob_sha"]:
                raise ValueError(f"remote file content differs from accepted worker evidence: {path}")
        else:
            raise ValueError("worker evidence includes an unsupported file type")
    if seen != set(expected_files):
        raise ValueError("remote head is missing files from accepted worker evidence")

    head_sha = head_commit["sha"]
    marker = f"<!-- orbit-delivery:v1 request_id={request_id} review_sha256={review_hash} head_sha={head_sha} -->"
    title = admission.get("ticket", {}).get("title") or f"ORBIT: {repository} issue #{admission['issue']['number']}"
    body = "\n\n".join([
        marker,
        f"Implements {admission['issue']['url']}.",
        f"Pinned source: `{source_sha}`",
        f"Accepted diff SHA-256: `{evidence['diff_sha256']}`",
        f"Worker evidence receipt SHA-256: `{worker_receipt_sha256}`",
        f"Independent reviewer: @{review['reviewed_by']['login']} (review `{review_hash}`).",
        "This PR was created as a draft by an explicit operator request. It is not merged or deployed.",
    ])
    head_query = f"{repository.split('/')[0]}:{head_ref}"
    existing = _pulls(api, repository, head_query, base_ref)
    matches = [pr for pr in existing if marker in pr.get("body", "")]
    if matches:
        pr = matches[0]
    else:
        if existing:
            raise ValueError("a pull request already exists for this head/base without the matching ORBIT delivery receipt")
        payload = {"title": title, "body": body, "head": head_query, "base": base_ref, "draft": True}
        try:
            pr = api(f"repos/{repository}/pulls", method="POST", payload=payload)
        except ValueError:
            recovered = [value for value in _pulls(api, repository, head_query, base_ref) if marker in value.get("body", "")]
            if not recovered:
                raise
            pr = recovered[0]
    head = pr.get("head", {}) if isinstance(pr, dict) else {}
    base = pr.get("base", {}) if isinstance(pr, dict) else {}
    if (not isinstance(pr, dict) or type(pr.get("number")) is not int
            or head.get("sha") != head_sha or head.get("ref") != head_ref
            or base.get("ref") != base_ref or base.get("sha") != source_base_sha or pr.get("draft") is not True
            or marker not in pr.get("body", "") or not isinstance(pr.get("html_url"), str)):
        raise ValueError("GitHub draft PR failed head, base, state, or marker verification")
    delivered_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    connection.execute("BEGIN IMMEDIATE")
    try:
        prior = connection.execute("SELECT head_ref, base_ref, head_sha, pr_number FROM deliveries WHERE request_id = ?", (request_id,)).fetchone()
        expected = (head_ref, base_ref, head_sha, pr["number"])
        if prior and prior != expected:
            raise ValueError("delivery ledger conflicts with the verified GitHub pull request")
        if not prior:
            connection.execute("INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (request_id, head_ref, base_ref, head_sha, pr["number"], pr["html_url"], marker, delivered_at))
            connection.execute("INSERT INTO job_events (request_id, occurred_at, from_state, to_state, actor, details_json) VALUES (?, ?, 'ACCEPTED', 'DRAFT_PR', 'orbit-delivery', ?)", (request_id, delivered_at, json.dumps({"pr_number": pr["number"], "head_sha": head_sha, "review_sha256": review_hash}, sort_keys=True)))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"number": pr["number"], "url": pr["html_url"], "draft": True, "head_sha": head_sha, "review_sha256": review_hash}
