import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def run(args):
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
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError, json.JSONDecodeError, TypeError) as exc:
        parser.exit(2, f"ORBIT: {exc}\n")


if __name__ == "__main__":
    main()
