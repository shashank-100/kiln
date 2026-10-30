# kiln

Turn your coding-agent sessions into verified, replayable RL tasks.

kiln records an agent session in your repository, packages the bug it fixed as a
self-contained task, and proves the task can be trusted before anything trains on
it: the problem must exist at the start, the reference fix must pass, an empty
patch must fail, and a suite of reward attacks must all score zero.

## Workflow

```
kiln init                                   # register the repository
kiln doctor                                 # check prerequisites
kiln record -- claude -p "<bug report>"     # snapshot, run the agent, snapshot, link its log
kiln sessions                               # list recorded sessions
kiln task create --session <id>             # shows a draft instruction to review
kiln task create --session <id> --instruction-file reviewed.md
kiln task validate <task>                   # controls + attack suite
kiln grade <task> candidate.patch           # reward for any candidate patch
```

A task directory is portable: copy it anywhere and `kiln task validate <dir>` or
`kiln grade <dir> <patch>` works without the original repository or session.

## What makes a task trustworthy

**Capture.** Snapshots record the committed source as a plain archive, staged and
unstaged changes as separate binary patches, and untracked files, each hashed and
re-checked on restore. Credentials such as `.env` are never captured. Restored
workspaces contain a single commit, so no history before or after the snapshot is
reachable from inside a task.

**Instructions.** Task creation refuses an instruction that quotes the reference fix.

**Grading.** Every attempt runs in a freshly restored workspace. The candidate's
edits to tests and to test configuration (`conftest.py`, `pytest.ini`,
`pyproject.toml`, `setup.cfg`, `sitecustomize.py`, …) are dropped, the private
tests are applied, entry-point plugins are disabled, and git runs with hooks and
fsmonitor off. Outcomes come from a guard plugin loaded from outside the
workspace, which records results from raw call information and detects code under
test replacing pytest's reporting. Printed output is never parsed.

**Validation.** `kiln task validate` runs the starting state twice and the reference
fix three times (to find flaky tests), derives target and regression tests, then
grades the reference fix, an empty patch and eight attack patches:

| Attack | What it tries |
|---|---|
| forged-conftest | root `conftest.py` that marks every test passed |
| rewritten-target-tests | replaces the hidden tests with empty ones of the same name |
| deleted-tests | deletes the test files |
| shadowed-pytest | a `pytest.py` in the workspace that fakes the run and the report |
| plugin-via-config | `pytest.ini` loading a plugin shipped in the patch |
| in-process-report-patch | code under test rewrites pytest's verdicts at import time |
| escape-sandbox | code under test writes outside its workspace and opens a network connection |
| exit-before-report | code under test exits with status 0 before results are written |

A task is `valid` only if the reference scores 1, the empty patch scores 0 and
every attack scores 0. Results are `passed`, `failed` or `ungraded`, each with a
reason; infrastructure problems (setup failure, a task modified after validation)
are ungraded, never counted against the agent.

## Status

Implemented: capture, session recording and import (Claude Code), task creation,
verification and validation.

Known limits: isolation uses `sandbox-exec` on macOS and `bwrap` on Linux rather than
containers; Python and pytest only; one agent log format so far.
