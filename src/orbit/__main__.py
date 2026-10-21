import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from orbit import ledger
from orbit import worker

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT = {"schema_version": 1, "repository": "", "agent": {"builder": "unconfigured", "reviewer": "human"}, "limits": {"admissions_per_day": 30, "worker_attempts_per_day": 30, "max_concurrent": 1, "max_seconds_per_attempt": 600, "max_retries": 2}, "safety": {"stop": True, "auto_merge": False, "allow_deploy": False, "allow_destructive_cleanup": False}, "delivery": {"pull_request": "draft"}}


def validate(config):
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if not valid_repository(config.get("repository")):
        raise ValueError("repository must be owner/name")
    agent = config.get("agent")
    if not isinstance(agent, dict) or not isinstance(agent.get("builder"), str) or not isinstance(agent.get("reviewer"), str):
        raise ValueError("agent.builder and agent.reviewer must be strings")
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("limits missing")
    for name in ("admissions_per_day", "worker_attempts_per_day", "max_concurrent", "max_seconds_per_attempt", "max_retries"):
        v = limits.get(name)
        if type(v) is not int or v < (0 if name == "max_retries" else 1):
            raise ValueError(f"invalid {name}")
    if limits["admissions_per_day"] > limits["worker_attempts_per_day"]:
        raise ValueError("admissions cannot exceed worker attempts")
    safety = config.get("safety")
    if not isinstance(safety, dict) or safety.get("auto_merge") is not False or safety.get("allow_deploy") is not False or safety.get("allow_destructive_cleanup") is not False or safety.get("stop") is not True:
        raise ValueError("Phase 1 safety invariants must remain enabled")
    delivery = config.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("pull_request") != "draft":
        raise ValueError("Phase 1 delivery must be draft")
    return True


def valid_repository(value):
    if not isinstance(value, str) or not REPO_PATTERN.fullmatch(value):
        return False
    owner, name = value.split("/", 1)
    return owner not in (".", "..") and name not in (".", "..") and not name.endswith(".git")


def target_path(value):
    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise ValueError("target directory must not be a symlink")
    target = requested.resolve()
    if not target.is_dir():
        raise ValueError("target directory does not exist")
    return target


def git_output(target, *args):
    try:
        result = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, check=False, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git identity lookup timed out") from exc
    if result.returncode != 0:
        raise ValueError("target must be a Git checkout with an origin remote and a commit")
    return result.stdout.strip()


def github_remote_repository(remote):
    match = re.fullmatch(r"(?:https?://|ssh://git@|git@)?github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?", remote, re.IGNORECASE)
    if not match:
        raise ValueError("origin must point to a GitHub owner/name repository")
    return f"{match.group(1)}/{match.group(2)}"


def gh_api(route, method=None, payload=None):
    command = ["gh", "api", "--hostname", "github.com"]
    if method:
        command.extend(["--method", method])
    command.append(route)
    input_text = None
    if payload is not None:
        command.extend(["--input", "-"])
        input_text = json.dumps(payload)
    try:
        result = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False, timeout=30)
    except FileNotFoundError as exc:
        raise ValueError("GitHub CLI (gh) is required for authenticated ticket creation") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("GitHub API request timed out") from exc
    if result.returncode != 0:
        raise ValueError(f"GitHub API request failed: {result.stderr.strip() or 'unknown error'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub API returned invalid JSON") from exc


def default_policy_path():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "orbit" / "policy.json"


def default_ledger_path():
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "orbit" / "admissions.sqlite3"


def load_policy(path_value, repository):
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise ValueError("operator policy file is missing or is a symlink; create it outside the target repository")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("operator policy path must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("operator policy file must be owned by the current user")
        if metadata.st_mode & 0o022:
            raise ValueError("operator policy file must not be group or world writable")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            policy_bytes = stream.read()
        policy = json.loads(policy_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("operator policy file contains invalid JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
        raise ValueError("unsupported operator policy schema_version")
    if set(policy) != {"schema_version", "repositories"}:
        raise ValueError("operator policy fields do not match schema version 1")
    repositories = policy.get("repositories")
    if not isinstance(repositories, dict):
        raise ValueError("operator policy repositories must be an object")
    if any(not valid_repository(name) for name in repositories):
        raise ValueError("operator policy contains an invalid repository name")
    for entry in repositories.values():
        if not isinstance(entry, dict) or set(entry) - {"ticket_creators", "parent_issue", "admission"}:
            raise ValueError("operator policy repository fields do not match schema version 1")
    matching = [value for name, value in repositories.items() if isinstance(name, str) and name.casefold() == repository.casefold()]
    if len(matching) != 1 or not isinstance(matching[0], dict):
        raise ValueError("repository is not present in the operator policy")
    entry = matching[0]
    creators = entry.get("ticket_creators")
    if not isinstance(creators, list) or any(not isinstance(name, str) or not name.strip() for name in creators):
        raise ValueError("operator policy ticket_creators must be a list of GitHub logins")
    parent = entry.get("parent_issue")
    if parent is not None and (type(parent) is not int or parent < 1):
        raise ValueError("operator policy parent_issue must be a positive issue number or null")
    return policy, creators, parent, hashlib.sha256(policy_bytes).hexdigest()


def render_ticket(ticket):
    if not isinstance(ticket, dict) or type(ticket.get("schema_version")) is not int or ticket["schema_version"] != 1:
        raise ValueError("unsupported ticket schema_version")
    expected_fields = {"schema_version", "title", "objective", "scope", "acceptance_tests", "dependencies", "escalation_boundaries"}
    if set(ticket) != expected_fields:
        raise ValueError("ticket request fields do not match schema version 1")
    title = ticket.get("title")
    objective = ticket.get("objective")
    scope = ticket.get("scope")
    acceptance = ticket.get("acceptance_tests")
    dependencies = ticket.get("dependencies")
    escalation = ticket.get("escalation_boundaries")
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("ticket title must contain 1 to 256 characters")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("ticket objective is required")
    if not isinstance(scope, dict):
        raise ValueError("ticket scope must be an object")
    if set(scope) != {"allowed_paths", "prohibited_changes"}:
        raise ValueError("ticket scope fields do not match schema version 1")
    allowed = scope.get("allowed_paths")
    prohibited = scope.get("prohibited_changes")
    if not isinstance(allowed, list) or not allowed or any(not isinstance(value, str) or not value.strip() for value in allowed):
        raise ValueError("ticket scope.allowed_paths must be a non-empty list of paths")
    if not isinstance(prohibited, list) or any(not isinstance(value, str) or not value.strip() for value in prohibited):
        raise ValueError("ticket scope.prohibited_changes must be a list of non-empty descriptions")
    if not isinstance(acceptance, list) or not acceptance or any(not isinstance(value, str) or not value.strip() for value in acceptance):
        raise ValueError("ticket acceptance_tests must be a non-empty list")
    if not isinstance(dependencies, str) or not isinstance(escalation, str) or not escalation.strip():
        raise ValueError("ticket dependencies and escalation_boundaries must be strings; escalation_boundaries is required")
    lines = ["## Objective and context", objective.strip(), "", "## Allowed paths", *[f"- `{value.strip()}`" for value in allowed], "", "## Prohibited changes", *([f"- {value.strip()}" for value in prohibited] or ["- None specified"]), "", "## Acceptance tests and evidence", *[f"- {value.strip()}" for value in acceptance], "", "## Dependencies", dependencies.strip() or "None", "", "## Escalation boundaries", escalation.strip()]
    return {"title": title.strip(), "body": "\n".join(lines), "ticket": ticket}


def load_ticket(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("ticket request must be an existing regular file, not a symlink")
    try:
        ticket = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("ticket request contains invalid JSON") from exc
    return render_ticket(ticket)


def canonical_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_request(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("request must be an existing regular file, not a symlink")
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("request contains invalid JSON") from exc
    if not isinstance(request, dict) or type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise ValueError("unsupported request schema_version")
    if request.get("state") != "PROPOSED":
        raise ValueError("only PROPOSED requests can be admitted")
    if set(request) != {"schema_version", "request_id", "state", "repository", "issue", "source", "ticket", "requested_by", "authorization", "created_at"}:
        raise ValueError("request envelope fields do not match schema version 1")
    try:
        if str(uuid.UUID(request.get("request_id", ""))) != request["request_id"]:
            raise ValueError("request_id must be a canonical UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("request_id must be a canonical UUID") from exc
    repo = request.get("repository")
    issue = request.get("issue")
    source = request.get("source")
    authorization = request.get("authorization")
    requester = request.get("requested_by")
    ticket = request.get("ticket")
    if (isinstance(repo, dict) and set(repo) != {"full_name", "id"}
            or isinstance(issue, dict) and set(issue) != {"number", "id", "url", "body_sha256", "parent_issue"}
            or isinstance(source, dict) and set(source) != {"sha"}
            or isinstance(authorization, dict) and set(authorization) != {"method", "scope", "job_admission", "policy_sha256"}
            or isinstance(requester, dict) and set(requester) != {"login", "id"}):
        raise ValueError("request envelope fields do not match schema version 1")
    if (not isinstance(repo, dict) or not valid_repository(repo.get("full_name")) or type(repo.get("id")) is not int
            or not isinstance(issue, dict) or type(issue.get("id")) is not int or type(issue.get("number")) is not int
            or not isinstance(issue.get("url"), str) or not re.fullmatch(r"[0-9a-f]{64}", issue.get("body_sha256", ""))
            or (issue.get("parent_issue") is not None and (type(issue["parent_issue"]) is not int or issue["parent_issue"] < 1))
            or not isinstance(source, dict) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source.get("sha", ""))
            or not isinstance(authorization, dict) or authorization.get("method") != "github_authenticated_principal_allowlist"
            or authorization.get("scope") != "ticket_creation_only" or authorization.get("job_admission") != "not_authorized"
            or not re.fullmatch(r"[0-9a-f]{64}", authorization.get("policy_sha256", ""))
            or not isinstance(requester, dict) or not isinstance(requester.get("login"), str) or type(requester.get("id")) is not int
            or type(repo.get("id")) is not int or repo["id"] < 1 or issue["id"] < 1 or issue["number"] < 1
            or requester["id"] < 1 or not isinstance(request.get("created_at"), str)):
        raise ValueError("request envelope is incomplete or malformed")
    if issue["url"].casefold() != f"https://github.com/{repo['full_name']}/issues/{issue['number']}".casefold():
        raise ValueError("request issue URL does not match its repository and issue number")
    rendered = render_ticket(ticket)
    return request, rendered


def admission_settings(policy, repository, allow_stopped=False):
    repositories = policy.get("repositories")
    matching = [value for name, value in repositories.items() if isinstance(name, str) and name.casefold() == repository.casefold()]
    if len(matching) != 1 or not isinstance(matching[0], dict):
        raise ValueError("repository is not present in the operator policy")
    entry = matching[0]
    admission = entry.get("admission")
    if not isinstance(admission, dict):
        raise ValueError("operator policy has no admission settings")
    if set(admission) != {"approvers", "stop", "limits"}:
        raise ValueError("operator policy admission fields do not match schema version 1")
    approvers = admission.get("approvers")
    if not isinstance(approvers, list) or any(not isinstance(name, str) or not name.strip() for name in approvers):
        raise ValueError("operator policy admission.approvers must be a list of GitHub logins")
    stop = admission.get("stop", True)
    if type(stop) is not bool:
        raise ValueError("operator policy admission.stop must be a boolean")
    if stop and not allow_stopped:
        raise ValueError("admission is stopped by operator policy")
    limits = admission.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("operator policy admission.limits is required")
    expected_limits = {
        "admissions_per_day", "worker_attempts_per_day", "max_concurrent",
        "max_seconds_per_attempt", "max_retries", "max_request_lifetime_seconds", "max_ticket_age_seconds",
    }
    if set(limits) != expected_limits:
        raise ValueError("operator policy admission limits do not match schema version 1")
    for name in expected_limits:
        value = limits.get(name)
        if type(value) is not int or value < (0 if name == "max_retries" else 1):
            raise ValueError(f"invalid operator policy admission.limits.{name}")
    parent = entry.get("parent_issue")
    if parent is not None and (type(parent) is not int or parent < 1):
        raise ValueError("operator policy parent_issue must be a positive issue number or null")
    return entry, admission, approvers, limits, parent


def secure_ledger_path(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise ValueError("admission ledger path must not be a symlink")
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("admission ledger directory must be a real directory")
    if hasattr(os, "getuid"):
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            raise ValueError("admission ledger directory must be private and owned by the current user")
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    if path.is_symlink() or not path.is_file():
        raise ValueError("admission ledger must be a regular file")
    if hasattr(os, "getuid"):
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("admission ledger file must be private and owned by the current user")
    return path


def record_admission(receipt, ledger_path, limits):
    path = secure_ledger_path(ledger_path)
    try:
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    except sqlite3.Error as exc:
        raise ValueError(f"could not open admission ledger: {exc}") from exc
    try:
        ledger.initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        now = receipt["admitted_at"]
        utc_day = now[:10]
        repo_id = receipt["repository"]["id"]
        count = connection.execute("SELECT COUNT(*) FROM admissions WHERE repository_id = ? AND substr(admitted_at, 1, 10) = ?", (repo_id, utc_day)).fetchone()[0]
        if count >= limits["admissions_per_day"]:
            raise ValueError("daily admission budget exhausted")
        active = connection.execute("SELECT COUNT(*) FROM admissions WHERE repository_id = ? AND expires_at > ?", (repo_id, now)).fetchone()[0]
        if active >= limits["max_concurrent"]:
            raise ValueError("concurrent admission limit reached")
        connection.execute(
            "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt["request_id"], receipt["repository"]["id"], receipt["issue"]["id"], receipt["issue"]["number"], receipt["request_sha256"], receipt["policy_sha256"], receipt["admitted_by"]["login"], now, receipt["expires_at"], json.dumps(receipt, sort_keys=True)),
        )
        ledger.enqueue_admitted(connection, receipt)
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ValueError("request or issue has already been admitted") from exc
    except sqlite3.Error as exc:
        connection.rollback()
        raise ValueError(f"admission ledger operation failed: {exc}") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def admit_request(args):
    request, rendered = load_request(args.request)
    target = target_path(args.target)
    orbit_dir = target / ".orbit"
    config_file = orbit_dir / "config.json"
    if orbit_dir.is_symlink() or config_file.is_symlink() or not config_file.is_file():
        raise ValueError("missing safe .orbit/config.json")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    validate(config)
    repository = config["repository"]
    if repository.casefold() != request["repository"]["full_name"].casefold():
        raise ValueError("request repository does not match target configuration")
    if github_remote_repository(git_output(target, "remote", "get-url", "origin")).casefold() != repository.casefold():
        raise ValueError("Git origin does not match the configured repository")
    if git_output(target, "rev-parse", "HEAD") != request["source"]["sha"]:
        raise ValueError("request source SHA is stale for the target checkout")

    policy_path = Path(args.policy or default_policy_path()).expanduser()
    if policy_path.resolve() == target or target in policy_path.resolve().parents:
        raise ValueError("operator policy must be stored outside the target repository")
    policy, ticket_creators, _, policy_hash = load_policy(policy_path, repository)
    if policy_hash != request["authorization"]["policy_sha256"]:
        raise ValueError("request policy hash is stale; create a new ticket under the current policy")
    if request["requested_by"]["login"].casefold() not in {name.casefold() for name in ticket_creators}:
        raise ValueError("ticket creator is not authorized by the current operator policy")
    _, _, approvers, limits, parent_number = admission_settings(policy, repository)
    if request["issue"].get("parent_issue") != parent_number:
        raise ValueError("request parent does not match the configured parent policy")
    ttl = limits["max_request_lifetime_seconds"] if args.expires_seconds is None else args.expires_seconds
    if type(ttl) is not int or ttl < 1 or ttl > limits["max_request_lifetime_seconds"]:
        raise ValueError("requested expiry is outside the operator policy limit")
    request_hash = canonical_hash(request)
    if args.confirm_request_sha256 != request_hash:
        raise ValueError(f"review request details, then repeat with --confirm-request-sha256 {request_hash}")

    actor = gh_api("user")
    if not isinstance(actor, dict) or not isinstance(actor.get("login"), str) or type(actor.get("id")) is not int:
        raise ValueError("GitHub did not return an authenticated principal")
    if actor["login"].casefold() not in {name.casefold() for name in approvers}:
        raise ValueError("authenticated GitHub user is not in admission.approvers")
    repo_data = gh_api(f"repos/{repository}")
    if (not isinstance(repo_data, dict) or not isinstance(repo_data.get("full_name"), str)
            or repo_data["full_name"].casefold() != repository.casefold()
            or type(repo_data.get("id")) is not int or repo_data["id"] != request["repository"]["id"]):
        raise ValueError("GitHub repository identity does not match the immutable request")
    commit = gh_api(f"repos/{repository}/commits/{request['source']['sha']}")
    if not isinstance(commit, dict) or commit.get("sha") != request["source"]["sha"]:
        raise ValueError("pinned source commit is missing or changed in GitHub")
    issue = gh_api(f"repos/{repository}/issues/{request['issue']['number']}")
    expected_url = f"https://github.com/{repository}/issues/{request['issue']['number']}"
    marker = f"<!-- orbit-request:v1 id={request['request_id']} source_sha={request['source']['sha']} policy_sha256={policy_hash} -->"
    expected_body = rendered["body"] + f"\n\n{marker}\n"
    issue_user = issue.get("user") if isinstance(issue, dict) else None
    if (not isinstance(issue, dict) or type(issue.get("id")) is not int or issue["id"] != request["issue"]["id"]
            or type(issue.get("number")) is not int or issue["number"] != request["issue"]["number"]
            or issue.get("state") != "open" or "pull_request" in issue
            or not isinstance(issue.get("html_url"), str) or issue["html_url"].casefold() != expected_url.casefold()
            or not isinstance(issue_user, dict) or not isinstance(issue_user.get("login"), str)
            or issue_user["login"].casefold() != request["requested_by"]["login"].casefold()
            or type(issue_user.get("id")) is not int or issue_user["id"] != request["requested_by"]["id"]
            or issue.get("body") != expected_body
            or hashlib.sha256(expected_body.encode("utf-8")).hexdigest() != request["issue"]["body_sha256"]
            or issue.get("created_at") != request.get("created_at")):
        raise ValueError("live GitHub issue failed identity, state, creator, source, or body verification")
    if parent_number is not None:
        parent = gh_api(f"repos/{repository}/issues/{request['issue']['number']}/parent")
        if not isinstance(parent, dict) or type(parent.get("number")) is not int or parent["number"] != parent_number:
            raise ValueError("live GitHub issue parent does not match operator policy")

    now = datetime.now(timezone.utc)
    try:
        ticket_created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("live GitHub issue has an invalid creation timestamp") from exc
    if ticket_created.tzinfo is None or (now - ticket_created).total_seconds() > limits["max_ticket_age_seconds"] or ticket_created > now + timedelta(minutes=5):
        raise ValueError("GitHub ticket is stale or has a future creation timestamp")
    admitted_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    expires_at = (now + timedelta(seconds=ttl)).isoformat(timespec="seconds").replace("+00:00", "Z")
    receipt = {
        "schema_version": 1,
        "receipt_id": str(uuid.uuid4()),
        "request_id": request["request_id"],
        "state": "ADMITTED",
        "request_sha256": request_hash,
        "policy_sha256": policy_hash,
        "repository": request["repository"],
        "issue": request["issue"],
        "source": request["source"],
        "ticket": request["ticket"],
        "scope": request["ticket"]["scope"],
        "operation": "code_change",
        "policy": {"sha256": policy_hash, "parent_issue": parent_number, "admission_limits": limits},
        "admitted_by": {"login": actor["login"], "id": actor["id"]},
        "admitted_at": admitted_at,
        "expires_at": expires_at,
    }
    ledger_path = Path(args.ledger or default_ledger_path()).expanduser()
    resolved_ledger = ledger_path.resolve()
    if resolved_ledger == target or target in resolved_ledger.parents:
        raise ValueError("admission ledger must be stored outside the target repository")
    record_admission(receipt, ledger_path, limits)
    return receipt


def run_ticket_create(args):
    target = target_path(args.target)
    orbit_dir = target / ".orbit"
    config_file = orbit_dir / "config.json"
    if orbit_dir.is_symlink() or config_file.is_symlink() or not config_file.is_file():
        raise ValueError("missing safe .orbit/config.json; run orbit init")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    validate(config)
    ticket = load_ticket(args.request)

    repository = config["repository"]
    remote_repository = github_remote_repository(git_output(target, "remote", "get-url", "origin"))
    source_sha = git_output(target, "rev-parse", "HEAD")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_sha):
        raise ValueError("could not pin a valid source commit")
    if remote_repository.casefold() != repository.casefold():
        raise ValueError("Git origin does not match the configured repository")

    policy_path = Path(args.policy or default_policy_path()).expanduser()
    resolved_policy = policy_path.resolve()
    if resolved_policy == target or target in resolved_policy.parents:
        raise ValueError("operator policy must be stored outside the target repository")
    _, allowlist, parent_number, policy_sha256 = load_policy(policy_path, repository)

    actor = gh_api("user")
    if not isinstance(actor, dict) or not isinstance(actor.get("login"), str) or type(actor.get("id")) is not int:
        raise ValueError("GitHub did not return an authenticated principal")
    if actor["login"].casefold() not in {name.casefold() for name in allowlist}:
        raise ValueError("authenticated GitHub user is not in the operator ticket-creator allowlist")

    repo_data = gh_api(f"repos/{repository}")
    if not isinstance(repo_data, dict) or not valid_repository(repo_data.get("full_name")) or repo_data["full_name"].casefold() != repository.casefold() or type(repo_data.get("id")) is not int:
        raise ValueError("GitHub repository identity does not match configuration")

    parent_issue = None
    payload = {"title": ticket["title"], "body": ticket["body"]}
    if parent_number is not None:
        parent_issue = gh_api(f"repos/{repository}/issues/{parent_number}")
        if (not isinstance(parent_issue, dict) or type(parent_issue.get("id")) is not int
                or type(parent_issue.get("number")) is not int or parent_issue["number"] != parent_number):
            raise ValueError("configured parent issue does not exist in the configured repository")
        payload["parent_issue_id"] = parent_issue["id"]

    request_id = str(uuid.uuid4())
    marker = f"\n\n<!-- orbit-request:v1 id={request_id} source_sha={source_sha} policy_sha256={policy_sha256} -->\n"
    payload["body"] += marker
    created = gh_api(f"repos/{repository}/issues", method="POST", payload=payload)
    if not isinstance(created, dict) or type(created.get("id")) is not int or type(created.get("number")) is not int:
        raise ValueError("GitHub created a ticket but returned no verifiable issue identity")

    issue = gh_api(f"repos/{repository}/issues/{created['number']}")
    expected_url = f"https://github.com/{repository}/issues/{created['number']}"
    issue_user = issue.get("user") if isinstance(issue, dict) else None
    if (not isinstance(issue, dict) or issue.get("id") != created["id"] or issue.get("number") != created["number"]
            or not isinstance(issue.get("html_url"), str) or issue["html_url"].casefold() != expected_url.casefold()
            or not isinstance(issue_user, dict) or not isinstance(issue_user.get("login"), str)
            or issue_user["login"].casefold() != actor["login"].casefold() or issue_user.get("id") != actor["id"]
            or issue.get("body") != payload["body"]):
        raise ValueError(f"GitHub created {expected_url}, but its identity, creator, or body failed verification")

    verified_parent = None
    if parent_number is not None:
        verified_parent = gh_api(f"repos/{repository}/issues/{created['number']}/parent")
        if not isinstance(verified_parent, dict) or verified_parent.get("id") != parent_issue["id"] or verified_parent.get("number") != parent_number:
            raise ValueError(f"GitHub created {expected_url}, but it does not have the configured parent relationship")

    return {
        "schema_version": 1,
        "request_id": request_id,
        "state": "PROPOSED",
        "repository": {"full_name": repo_data["full_name"], "id": repo_data["id"]},
        "issue": {
            "number": issue["number"],
            "id": issue["id"],
            "url": issue["html_url"],
            "body_sha256": hashlib.sha256(payload["body"].encode("utf-8")).hexdigest(),
            "parent_issue": verified_parent["number"] if verified_parent else None,
        },
        "source": {"sha": source_sha},
        "ticket": ticket["ticket"],
        "requested_by": {"login": actor["login"], "id": actor["id"]},
        "authorization": {
            "method": "github_authenticated_principal_allowlist",
            "scope": "ticket_creation_only",
            "job_admission": "not_authorized",
            "policy_sha256": policy_sha256,
        },
        "created_at": issue.get("created_at"),
    }


def write_request(path_value, request):
    path = Path(path_value).expanduser()
    if path.exists() or path.is_symlink():
        raise ValueError("request output already exists; refusing overwrite")
    parent = path.parent.resolve()
    if not parent.is_dir():
        raise ValueError("request output directory does not exist")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent, prefix=".orbit-request-", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(request, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, path)
    except FileExistsError as exc:
        raise ValueError("request output already exists; refusing overwrite") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def open_ledger(path_value):
    path = secure_ledger_path(path_value)
    try:
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        ledger.initialize(connection)
        return connection
    except sqlite3.Error as exc:
        raise ValueError(f"could not open queue ledger: {exc}") from exc


def repository_id_from_ledger(connection, repository):
    for repository_id, receipt_json in connection.execute("SELECT DISTINCT repository_id, receipt_json FROM admissions"):
        receipt = json.loads(receipt_json)
        full_name = receipt.get("repository", {}).get("full_name", "")
        if isinstance(full_name, str) and full_name.casefold() == repository.casefold():
            return repository_id
    return None


def queue_target_policy(args, allow_stopped=False):
    target = target_path(args.target)
    config_file = target / ".orbit" / "config.json"
    if (target / ".orbit").is_symlink() or config_file.is_symlink() or not config_file.is_file():
        raise ValueError("missing safe .orbit/config.json")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    validate(config)
    repository = config["repository"]
    if github_remote_repository(git_output(target, "remote", "get-url", "origin")).casefold() != repository.casefold():
        raise ValueError("Git origin does not match the configured repository")
    policy_path = Path(args.policy or default_policy_path()).expanduser()
    if policy_path.resolve() == target or target in policy_path.resolve().parents:
        raise ValueError("operator policy must be stored outside the target repository")
    policy, _, _, policy_hash = load_policy(policy_path, repository)
    _, _, _, limits, _ = admission_settings(policy, repository, allow_stopped=allow_stopped)
    return target, repository, policy_hash, limits


def queue_claim(args):
    _, repository, policy_hash, limits = queue_target_policy(args)
    connection = open_ledger(args.ledger or default_ledger_path())
    try:
        repository_id = repository_id_from_ledger(connection, repository)
        if repository_id is None:
            return None
        return ledger.claim_next(
            connection, repository_id, args.worker_id, args.claim_key, policy_hash,
            limits, stop=False, lease_seconds=args.lease_seconds,
        )
    finally:
        connection.close()


def queue_status(args):
    target = target_path(args.target)
    config_file = target / ".orbit" / "config.json"
    if (target / ".orbit").is_symlink() or config_file.is_symlink() or not config_file.is_file():
        raise ValueError("missing safe .orbit/config.json")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    validate(config)
    connection = open_ledger(args.ledger or default_ledger_path())
    try:
        repository_id = repository_id_from_ledger(connection, config["repository"])
        if repository_id is None:
            return {"admissions": 0, "jobs": {}, "attempts": {}, "failed_validations": 0}
        return ledger.status(connection, repository_id)
    finally:
        connection.close()


def queue_heartbeat(args):
    _, _, policy_hash, _ = queue_target_policy(args)
    connection = open_ledger(args.ledger or default_ledger_path())
    try:
        return ledger.heartbeat(connection, args.request_id, args.lease_token, lease_seconds=args.lease_seconds, policy_sha256=policy_hash)
    finally:
        connection.close()


def queue_finish(args):
    _, repository, _, _ = queue_target_policy(args, allow_stopped=True)
    connection = open_ledger(args.ledger or default_ledger_path())
    try:
        repository_id = repository_id_from_ledger(connection, repository)
        if repository_id is None:
            raise ValueError("no ledger admission exists for this repository")
        return ledger.finish_attempt(
            connection, args.request_id, args.lease_token,
            success=args.outcome == "succeeded", details={"summary": args.summary or ""},
            max_retries=None,
        )
    finally:
        connection.close()


def run_worker(args):
    _, repository, policy_hash, limits = queue_target_policy(args)
    command = worker.load_argv(args.command_json)
    test_command = worker.load_argv(args.test_command_json)
    connection = open_ledger(args.ledger or default_ledger_path())
    try:
        repository_id = repository_id_from_ledger(connection, repository)
        if repository_id is None:
            return None
        claim = ledger.claim_next(
            connection, repository_id, args.worker_id, args.claim_key, policy_hash,
            limits, stop=False, lease_seconds=min(args.lease_seconds, limits["max_seconds_per_attempt"]),
        )
        if claim is None:
            return None
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "orbit" / "worker"
        try:
            return worker.run_claimed_job(
                target_path(args.target), repository_id, repository,
                Path(args.policy or default_policy_path()).expanduser(), policy_hash,
                limits, connection, claim, command, test_command, state_home,
            )
        except Exception as exc:
            try:
                ledger.finish_attempt(
                    connection, claim["job_id"], claim["lease_token"], False,
                    details={"failure_reason": str(exc), "worker_setup_failed": True}, max_retries=None, retryable=False,
                )
            except Exception:
                pass
            raise ValueError(f"worker attempt blocked before completion: {exc}") from exc
    finally:
        connection.close()


def log_failed_validation(args, reason):
    try:
        request = json.loads(Path(args.request).expanduser().read_text(encoding="utf-8"))
        request_id = request.get("request_id") if isinstance(request, dict) and isinstance(request.get("request_id"), str) else None
        repository = request.get("repository") if isinstance(request, dict) else None
        repository_id = repository.get("id") if isinstance(repository, dict) and type(repository.get("id")) is int else None
        if repository_id is None:
            return
        connection = open_ledger(args.ledger or default_ledger_path())
        try:
            ledger.record_validation(connection, repository_id, request_id, reason)
        finally:
            connection.close()
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error):
        return


def run(args):
    if args.command == "admit":
        receipt = admit_request(args)
        print(json.dumps(receipt, indent=2))
        return
    if args.command == "ticket":
        request = run_ticket_create(args)
        if args.output:
            try:
                write_request(args.output, request)
            except (OSError, ValueError) as exc:
                raise ValueError(f"ticket was created at {request['issue']['url']}, but request output failed: {exc}") from exc
            print(f"ORBIT request written: {args.output}")
        else:
            print(json.dumps(request, indent=2))
        return
    if args.command == "queue":
        if args.queue_command == "claim":
            result = queue_claim(args)
        elif args.queue_command == "status":
            result = queue_status(args)
        elif args.queue_command == "heartbeat":
            result = queue_heartbeat(args)
        else:
            result = queue_finish(args)
        print(json.dumps(result, indent=2))
        return
    if args.command == "worker":
        result = run_worker(args)
        print(json.dumps(result, indent=2))
        return
    target = target_path(args.target)
    orbit_dir = target / ".orbit"
    config_file = orbit_dir / "config.json"
    if args.command == "init":
        if not valid_repository(args.repo):
            raise ValueError("repository must be owner/name")
        if orbit_dir.exists() or orbit_dir.is_symlink():
            raise ValueError("ORBIT bootstrap files already exist; refusing overwrite")
        config = json.loads(json.dumps(DEFAULT))
        config["repository"] = args.repo
        validate(config)
        staging = Path(tempfile.mkdtemp(prefix=".orbit-init-", dir=target))
        try:
            (staging / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            (staging / "AGENT_BOOTSTRAP.md").write_text("# Fresh Agent Bootstrap\n\nRead the repository AGENTS.md if present, .orbit/config.json, current GitHub issue, relevant docs and tests. Confirm remote, branch and HEAD. Never assume previous conversation context. Stay within approved scope and preserve existing worktrees and untracked files. No automatic merge or deployment.\n", encoding="utf-8")
            if orbit_dir.exists() or orbit_dir.is_symlink():
                raise ValueError("ORBIT bootstrap files already exist; refusing overwrite")
            os.rename(staging, orbit_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        print(f"ORBIT initialized: {config_file}")
    else:
        if orbit_dir.is_symlink() or config_file.is_symlink():
            raise ValueError("ORBIT configuration paths must not be symlinks")
        if not config_file.is_file():
            raise ValueError("missing .orbit/config.json; run orbit init")
        config = json.loads(config_file.read_text(encoding="utf-8"))
        validate(config)
        print(f"ORBIT config valid for {config['repository']}")
        if args.command == "doctor":
            print(f"Python: {sys.version_info.major}.{sys.version_info.minor}")
            for name in ("git", "gh"):
                executable = shutil.which(name)
                print(f"{name}: {executable or 'not installed (optional for bootstrap)'}")
            if shutil.which("git"):
                check = subprocess.run(["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False)
                print(f"Git repository: {check.returncode == 0 and check.stdout.strip() == 'true'}")


def main():
    parser = argparse.ArgumentParser(prog="orbit", description="ORBIT bootstrap, ticket proposal, and admission CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--repo", required=True)
    for name in ("init", "doctor", "validate"):
        p = init if name == "init" else sub.add_parser(name)
        p.add_argument("--target", required=True)
    ticket = sub.add_parser("ticket")
    ticket_commands = ticket.add_subparsers(dest="ticket_command", required=True)
    create = ticket_commands.add_parser("create")
    create.add_argument("--target", required=True)
    create.add_argument("--request", required=True, help="versioned JSON task request")
    create.add_argument("--output")
    create.add_argument("--policy", help="operator policy JSON outside the target repository")
    admit = sub.add_parser("admit", help="explicitly approve one verified request")
    admit.add_argument("--target", required=True)
    admit.add_argument("--request", required=True)
    admit.add_argument("--policy", help="operator policy JSON outside the target repository")
    admit.add_argument("--ledger", help="private SQLite admission ledger path")
    admit.add_argument("--expires-seconds", type=int)
    admit.add_argument("--confirm-request-sha256", help="reviewed canonical request hash; required to authorize")
    queue = sub.add_parser("queue", help="inspect and operate durable job leases")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    claim = queue_commands.add_parser("claim", help="claim one admitted job")
    claim.add_argument("--target", required=True)
    claim.add_argument("--policy")
    claim.add_argument("--ledger")
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--claim-key", required=True, help="stable key for an idempotent claim request")
    claim.add_argument("--lease-seconds", type=int, default=60)
    status = queue_commands.add_parser("status", help="show queue, attempt, and validation counts")
    status.add_argument("--target", required=True)
    status.add_argument("--ledger")
    heartbeat = queue_commands.add_parser("heartbeat", help="renew a live lease within its attempt deadline")
    heartbeat.add_argument("--target", required=True)
    heartbeat.add_argument("--policy")
    heartbeat.add_argument("--ledger")
    heartbeat.add_argument("--request-id", required=True)
    heartbeat.add_argument("--lease-token", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=60)
    finish = queue_commands.add_parser("finish", help="record a worker attempt result")
    finish.add_argument("--target", required=True)
    finish.add_argument("--policy")
    finish.add_argument("--ledger")
    finish.add_argument("--request-id", required=True)
    finish.add_argument("--lease-token", required=True)
    finish.add_argument("--outcome", choices=("succeeded", "failed"), required=True)
    finish.add_argument("--summary")
    worker_parser = sub.add_parser("worker", help="run one explicitly claimed job in an isolated worktree")
    worker_parser.add_argument("--target", required=True)
    worker_parser.add_argument("--policy")
    worker_parser.add_argument("--ledger")
    worker_parser.add_argument("--worker-id", required=True)
    worker_parser.add_argument("--claim-key", required=True)
    worker_parser.add_argument("--command-json", required=True, help="private JSON argv array; no shell")
    worker_parser.add_argument("--test-command-json", required=True, help="private JSON argv array for acceptance tests")
    worker_parser.add_argument("--lease-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError, json.JSONDecodeError, TypeError) as exc:
        if args.command == "admit":
            log_failed_validation(args, str(exc))
        parser.exit(2, f"ORBIT: {exc}\n")


if __name__ == "__main__":
    main()
