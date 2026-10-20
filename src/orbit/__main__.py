import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT = {"schema_version": 1, "repository": "", "agent": {"builder": "unconfigured", "reviewer": "human"}, "limits": {"admissions_per_day": 30, "worker_attempts_per_day": 30, "max_concurrent": 1, "max_seconds_per_attempt": 600, "max_retries": 2}, "safety": {"stop": True, "auto_merge": False, "allow_deploy": False, "allow_destructive_cleanup": False}, "delivery": {"pull_request": "draft"}}


def validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("unsupported schema_version")
    if not isinstance(config.get("repository"), str) or not REPO_PATTERN.fullmatch(config["repository"]):
        raise ValueError("repository must be owner/name")
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
    if config.get("delivery", {}).get("pull_request") != "draft":
        raise ValueError("Phase 1 delivery must be draft")
    return True


def target_path(value):
    target = Path(value).expanduser().resolve()
    if not target.is_dir():
        raise ValueError("target directory does not exist")
    return target


def run(args):
    target = target_path(args.target)
    config_file = target / ".orbit" / "config.json"
    if args.command == "init":
        if not REPO_PATTERN.fullmatch(args.repo):
            raise ValueError("repository must be owner/name")
        if config_file.exists() or (target / ".orbit" / "AGENT_BOOTSTRAP.md").exists():
            raise ValueError("ORBIT bootstrap files already exist; refusing overwrite")
        config = json.loads(json.dumps(DEFAULT))
        config["repository"] = args.repo
        validate(config)
        config_file.parent.mkdir(exist_ok=True)
        config_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        (config_file.parent / "AGENT_BOOTSTRAP.md").write_text("# Fresh Agent Bootstrap\n\nRead the repository AGENTS.md if present, .orbit/config.json, current GitHub issue, relevant docs and tests. Confirm remote, branch and HEAD. Never assume previous conversation context. Stay within approved scope and preserve existing worktrees and untracked files. No automatic merge or deployment.\n", encoding="utf-8")
        print(f"ORBIT initialized: {config_file}")
    else:
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
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"ORBIT: {exc}\n")


if __name__ == "__main__":
    main()
