#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
demo_repo="$(mktemp -d "${TMPDIR:-/tmp}/orbit-one-minute.XXXXXX")"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

git -C "$demo_repo" init --quiet
python3 -m orbit init --repo example/orbit-demo --target "$demo_repo"
python3 -m orbit validate --target "$demo_repo"
python3 -m orbit doctor --target "$demo_repo"

if python3 -m orbit init --repo example/orbit-demo --target "$demo_repo" >/dev/null 2>&1; then
  echo "ORBIT unexpectedly overwrote its existing bootstrap files" >&2
  exit 1
fi

python3 -m unittest discover -s "$repo_root/tests" -p 'test_worker.py' -v
printf '\nLocal demo passed. Temporary repository preserved at: %s\n' "$demo_repo"
printf 'This demo uses no GitHub API, credentials, agent provider, or live issue.\n'
