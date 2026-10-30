#!/usr/bin/env bash
# Record one real Claude Code session per toolbox bug, then package and validate
# each as a kiln task. Usage: fixtures/record_toolbox.sh <new-repo-dir>
set -euo pipefail
KILN="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$1"
uv run --project "$KILN" -q python "$KILN/fixtures/make_toolbox.py" "$REPO" >/dev/null
cd "$REPO"
kiln() { "$KILN/.venv/bin/kiln" "$@"; }
kiln init >/dev/null

uv run --project "$KILN" -q python -c "
import sys; sys.path.insert(0, '$KILN/fixtures')
from make_toolbox import BUGS
for b in BUGS: print('\t'.join(b))
" > .git/kiln-bugs.tsv

while IFS=$'\t' read -r id family testfile report; do
  echo "== $id ($family)"
  kiln record -- claude -p "Bug report: $report Please fix it, and add a regression test for exactly this case in $testfile. Only change what the fix needs." \
    --permission-mode acceptEdits --allowedTools Read Edit Write Glob Grep "Bash(python:*)" "Bash(python3:*)" </dev/null >/dev/null 2>&1 || true
  sid=$(kiln sessions | head -1 | awk '{print $1}')
  printf '%s\n' "$report" > ".git/kiln-instruction-$id.md"
  kiln task create --session "$sid" --id "$id" --family "$family" --instruction-file ".git/kiln-instruction-$id.md" | head -1 || true
  # Return to the clean starting state for the next bug (.kiln/ is excluded, never cleaned).
  git checkout -q -- . && git clean -qfd
done < .git/kiln-bugs.tsv
