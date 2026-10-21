import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

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
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "orbit" / "policy.json"


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
    repositories = policy.get("repositories")
    if not isinstance(repositories, dict):
        raise ValueError("operator policy repositories must be an object")
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


def load_ticket(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("ticket request must be an existing regular file, not a symlink")
    try:
        ticket = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("ticket request contains invalid JSON") from exc
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
    return {"title": title.strip(), "body": "\n".join(lines)}


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


def run(args):
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
    parser = argparse.ArgumentParser(prog="orbit", description="ORBIT standalone bootstrap (Phase 1)")
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
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError, json.JSONDecodeError, TypeError) as exc:
        parser.exit(2, f"ORBIT: {exc}\n")


if __name__ == "__main__":
    main()
