"""Explicit, bounded execution of an admitted request in a detached worktree."""

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from orbit import ledger


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _private_directory(path):
    path = Path(path).expanduser()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = path
    while current != current.parent:
        if current.is_symlink() or not current.is_dir():
            raise ValueError("worker state paths must be real directories")
        if current == path and hasattr(os, "getuid"):
            metadata = current.stat()
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ValueError("worker state directory must be private and owned by the current user")
            break
        current = current.parent
    return path.resolve()


def load_argv(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("worker command manifest must be a regular file, not a symlink")
    metadata = path.stat()
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("worker command manifest must be owned by the current user")
    if metadata.st_mode & 0o077:
        raise ValueError("worker command manifest must be private (mode 0600)")
    try:
        if metadata.st_size > 1024 * 1024:
            raise ValueError("worker command manifest exceeds 1 MiB")
        argv = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("worker command manifest must contain a JSON argv array") from exc
    if (not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not Path(argv[0]).is_absolute() or not os.access(argv[0], os.X_OK)):
        raise ValueError("worker command must be a non-empty argv array with an absolute executable path")
    return argv


def _read_operator_policy(path, repository):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("operator policy must remain a regular file")
    metadata = path.stat()
    if (hasattr(os, "getuid") and metadata.st_uid != os.getuid()) or metadata.st_mode & 0o022:
        raise ValueError("operator policy ownership or permissions changed")
    raw = path.read_bytes()
    policy = json.loads(raw.decode("utf-8"))
    entries = policy.get("repositories", {}) if isinstance(policy, dict) else {}
    entry = next((value for name, value in entries.items() if isinstance(name, str) and name.casefold() == repository.casefold()), None)
    if not isinstance(entry, dict) or not isinstance(entry.get("admission"), dict):
        raise ValueError("operator policy no longer contains this repository admission")
    return hashlib.sha256(raw).hexdigest(), entry["admission"]


def _git(target, *args, timeout=15, check=True):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-C", str(target), *args],
        capture_output=True, check=False, timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        raise ValueError(f"Git worktree operation failed: {result.stderr.decode(errors='replace')[:500]}")
    return result


def _reject_external_filters(target):
    result = _git(target, "config", "--local", "--get-regexp", r"^(filter\..*\.(smudge|process)|include(If\..*)?\.path)$", check=False)
    if result.returncode == 0 and result.stdout.strip():
        raise ValueError("target Git config defines external filters or includes; refusing unsafe checkout")


def _worktree_paths(target):
    result = _git(target, "worktree", "list", "--porcelain")
    return {line[9:] for line in result.stdout.decode("utf-8", errors="replace").splitlines() if line.startswith("worktree ")}


def _add_worktree(target, worktree, source_sha):
    if Path(worktree).exists() or Path(worktree).is_symlink():
        raise ValueError("worker worktree destination already exists; refusing overwrite")
    _reject_external_filters(target)
    before = _worktree_paths(target)
    _git(target, "worktree", "add", "--detach", str(worktree), source_sha, timeout=60)
    after = _worktree_paths(target)
    if not before.issubset(after) or str(Path(worktree).resolve()) not in after:
        raise ValueError("Git worktree inventory changed unexpectedly")
    return before


def _changed_paths(worktree):
    tracked = _git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--").stdout
    untracked = _git(worktree, "ls-files", "--others", "-z").stdout
    values = {value.decode("utf-8", errors="surrogateescape") for value in tracked.split(b"\0") + untracked.split(b"\0") if value}
    return sorted(values)


def _within_scope(paths, allowed):
    patterns = []
    for item in allowed:
        if not isinstance(item, str) or not item or item.startswith("/") or "\\" in item:
            raise ValueError("admitted scope contains an invalid path pattern")
        pure = PurePosixPath(item)
        if ".." in pure.parts or ".git" in pure.parts:
            raise ValueError("admitted scope contains an unsafe path pattern")
        patterns.append(item)
    bad = []
    for path in paths:
        pure = PurePosixPath(path)
        if ".." in pure.parts or ".git" in pure.parts or not any(
            path == pattern or (pattern.endswith("/") and path.startswith(pattern)) or fnmatchcase(path, pattern)
            for pattern in patterns
        ):
            bad.append(path)
    return bad


def _diff_hash(worktree, paths):
    digest = hashlib.sha256()
    changed_bytes = 0
    with tempfile.TemporaryFile() as diff_stream:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-C", str(worktree), "diff", "--binary", "--no-ext-diff", "--no-renames", "HEAD", "--"],
            stdout=diff_stream, stderr=subprocess.PIPE, timeout=30, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode:
            raise ValueError("could not calculate bounded worker diff evidence")
        diff_stream.seek(0)
        for block in iter(lambda: diff_stream.read(65536), b""):
            changed_bytes += len(block)
            if changed_bytes > 100 * 1024 * 1024:
                raise ValueError("worker diff exceeds the 100 MiB evidence limit")
            digest.update(block)
    for name in paths:
        path = Path(worktree, name)
        digest.update(b"\0" + name.encode("utf-8", errors="surrogateescape") + b"\0")
        if not path.exists() and not path.is_symlink():
            digest.update(b"DELETED")
        elif path.is_symlink():
            digest.update(b"SYMLINK:" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            file_size = path.stat().st_size
            changed_bytes += file_size
            if changed_bytes > 100 * 1024 * 1024:
                raise ValueError("worker diff exceeds the 100 MiB evidence limit")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"NONFILE")
    return digest.hexdigest()


def _changed_file_evidence(worktree, paths):
    object_format = _git(worktree, "rev-parse", "--show-object-format").stdout.decode().strip()
    algorithm = "sha256" if object_format == "sha256" else "sha1"
    files = {}
    for name in paths:
        path = Path(worktree, name)
        if not path.exists() and not path.is_symlink():
            files[name] = {"status": "removed", "blob_sha": None}
            continue
        if path.is_symlink():
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
            status = "symlink"
        elif path.is_file():
            if path.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("worker changed file exceeds the 100 MiB evidence limit")
            content = path.read_bytes()
            status = "present"
        else:
            files[name] = {"status": "nonfile", "blob_sha": None}
            continue
        blob = hashlib.new(algorithm)
        blob.update(f"blob {len(content)}\0".encode())
        blob.update(content)
        files[name] = {"status": status, "blob_sha": blob.hexdigest()}
    return files


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _kill_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _run_command(argv, cwd, home, output_dir, label, deadline, policy_path, repository, expected_policy_hash, heartbeat):
    stdout_path = output_dir / f"{label}.stdout"
    stderr_path = output_dir / f"{label}.stderr"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with open(stdout_path, "xb") as stdout, open(stderr_path, "xb") as stderr:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True)
        last_heartbeat = time.monotonic()
        stopped_reason = None
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped_reason = "timeout"
                _kill_process(process)
                break
            if stdout_path.stat().st_size + stderr_path.stat().st_size > 20 * 1024 * 1024:
                stopped_reason = "worker output exceeded the 20 MiB limit"
                _kill_process(process)
                break
            try:
                current_hash, admission = _read_operator_policy(policy_path, repository)
                if current_hash != expected_policy_hash or admission.get("stop") is not False:
                    stopped_reason = "operator STOP or policy change"
                    _kill_process(process)
                    break
            except Exception:
                stopped_reason = "operator policy became unreadable"
                _kill_process(process)
                break
            if time.monotonic() - last_heartbeat >= 20:
                try:
                    heartbeat()
                except Exception as exc:
                    stopped_reason = f"lease heartbeat failed: {exc}"
                    _kill_process(process)
                    break
                last_heartbeat = time.monotonic()
            try:
                process.wait(timeout=min(1.0, max(0.01, remaining)))
            except subprocess.TimeoutExpired:
                pass
        if stopped_reason is None and stdout_path.stat().st_size + stderr_path.stat().st_size > 20 * 1024 * 1024:
            stopped_reason = "worker output exceeded the 20 MiB limit"
    return {
        "returncode": process.returncode,
        "stopped_reason": stopped_reason,
        "stdout_sha256": _hash_file(stdout_path),
        "stderr_sha256": _hash_file(stderr_path),
        "stdout_bytes": stdout_path.stat().st_size,
        "stderr_bytes": stderr_path.stat().st_size,
    }


def run_claimed_job(target, repository_id, repository, policy_path, policy_hash, limits, connection, claim, command, test_command, state_root):
    request = claim["request"]
    request_id = claim["job_id"]
    source_sha = request.get("source", {}).get("sha")
    if not isinstance(source_sha, str) or _git(target, "rev-parse", f"{source_sha}^{{commit}}").stdout.decode().strip() != source_sha:
        raise ValueError("admitted source commit is missing from the target repository")
    if request.get("repository", {}).get("id") != repository_id or request.get("repository", {}).get("full_name", "").casefold() != repository.casefold():
        raise ValueError("admitted receipt repository identity does not match target")
    scope = request.get("scope")
    allowed = scope.get("allowed_paths") if isinstance(scope, dict) else None
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("admitted request has no enforceable allowed_paths")
    state_root = _private_directory(state_root)
    worktree_root = _private_directory(state_root / "worktrees" / str(repository_id))
    receipt_root = _private_directory(state_root / "receipts" / str(repository_id))
    attempt_dir = f"{hashlib.sha256(request_id.encode()).hexdigest()[:32]}-{claim['attempt']}"
    worktree = worktree_root / attempt_dir
    if worktree_root == target or target in worktree_root.parents or worktree_root in target.parents:
        raise ValueError("worker state and target checkout must be separate directory trees")
    started_at = _iso_now()
    preexisting_worktrees = _add_worktree(target, worktree, source_sha)
    home = _private_directory(worktree_root / f".home-{attempt_dir}")
    output_dir = _private_directory(receipt_root / f".output-{attempt_dir}")
    remaining_lifetime = (datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    if remaining_lifetime <= 0:
        raise ValueError("admission receipt expired before worker execution")
    deadline = time.monotonic() + min(limits["max_seconds_per_attempt"], remaining_lifetime)

    def renew():
        current_hash, admission = _read_operator_policy(policy_path, repository)
        if current_hash != policy_hash or admission.get("stop") is not False:
            raise ValueError("operator STOP or policy change")
        ledger.heartbeat(connection, request_id, claim["lease_token"], lease_seconds=min(60, limits["max_seconds_per_attempt"]), policy_sha256=policy_hash)

    commands = [("builder", command), ("tests", test_command)]
    results = []
    failure = None
    for label, argv in commands:
        if time.monotonic() >= deadline:
            failure = "attempt deadline exceeded before all commands completed"
            break
        try:
            result = _run_command(argv, worktree, home, output_dir, label, deadline, policy_path, repository, policy_hash, renew)
        except OSError as exc:
            failure = f"could not launch {label} command: {exc}"
            break
        results.append({"command": label, **result})
        if result["stopped_reason"]:
            failure = result["stopped_reason"]
            break
        if result["returncode"] != 0:
            failure = f"{label} command exited with status {result['returncode']}"
            break

    paths = _changed_paths(worktree)
    out_of_scope = _within_scope(paths, allowed)
    diff_hash = _diff_hash(worktree, paths)
    changed_files = _changed_file_evidence(worktree, paths)
    if out_of_scope:
        failure = "out-of-scope changes detected"
    ended_at = _iso_now()
    record = {
        "schema_version": 1,
        "request_id": request_id,
        "attempt": claim["attempt"],
        "repository": repository,
        "source_sha": source_sha,
        "worker_id": claim["worker_id"],
        "started_at": started_at,
        "ended_at": ended_at,
        "worktree": str(worktree),
        "output_directory": str(output_dir),
        "preexisting_worktrees_preserved": sorted(preexisting_worktrees),
        "command_sha256": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
        "test_command_sha256": hashlib.sha256(json.dumps(test_command, separators=(",", ":")).encode()).hexdigest(),
        "commands": results,
        "changed_paths": paths,
        "changed_files": changed_files,
        "out_of_scope_paths": out_of_scope,
        "diff_sha256": diff_hash,
        "outcome": "SUCCEEDED" if failure is None else "FAILED",
        "failure_reason": failure,
        "output_hashes_only": True,
    }
    record_bytes = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    receipt_path = receipt_root / f"{hashlib.sha256(request_id.encode()).hexdigest()[:32]}-{claim['attempt']}.json"
    descriptor = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(record_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    receipt_hash = hashlib.sha256(record_bytes).hexdigest()
    retryable = not out_of_scope and not (failure and (failure.startswith("operator ") or failure.startswith("lease heartbeat")))
    outcome = ledger.finish_attempt(
        connection, request_id, claim["lease_token"], success=failure is None,
        details={"worker_receipt": str(receipt_path), "worker_receipt_sha256": receipt_hash, "failure_reason": failure},
        max_retries=None, retryable=retryable,
    )
    return {"worker_receipt": record, "worker_receipt_sha256": receipt_hash, "ledger_result": outcome}
