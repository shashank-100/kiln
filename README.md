# kiln

Turn real Claude Code sessions in your repository into RL environments that another
agent can reset, attempt and receive a reward from, with the original session trace
kept as a demonstration.

Each environment is checked before use: the starting code must fail the target test,
the session's own fix must pass, an empty patch must fail, and eight known reward
attacks must all score zero. (Codex sessions are planned.)

## Workflow

```
kiln init                                   # register the repository
kiln doctor                                 # check prerequisites
kiln record -- claude -p "<bug report>"     # new session: snapshot, run the agent, snapshot, link its log
kiln traces import <claude-log.jsonl>       # past session: rebuild its start state and its own patch
kiln sessions                               # list sessions
kiln task create --session <id>             # shows a draft instruction to review
kiln task create --session <id> --instruction-file reviewed.md
kiln task validate <task>                   # controls + attack suite
kiln grade <task> candidate.patch           # reward for any candidate patch

kiln env reset <task> ./ws                  # fresh workspace for an agent
kiln env grade <task> ./ws                  # structured result for that workspace
kiln attempt <task> -- claude -p "{instruction}"   # reset, run any agent (answer hidden), grade
```

A task directory is portable: copy it anywhere and `kiln task validate <dir>` or
`kiln grade <dir> <patch>` works without the original repository or session.

## Using an environment

```python
from kiln.env import Env

with Env("path/to/task") as env:
    ws = env.reset()        # fresh copy of the starting state, environment set up
    print(env.instruction)
    ...                     # an agent edits files in ws
    r = env.grade()         # Result(status, reward, reason, diagnostics)
```

`status` is `passed` (reward 1.0), `failed` (0.0) or `ungraded` (None). Infrastructure
problems (failed setup, a task modified after validation, no sandbox, an agent that
could not be launched) are always ungraded, never counted as a failed attempt.
`diagnostics` holds per-test outcomes, the target tests, files dropped from the patch,
any tampering detected and the test log tail.

The candidate patch is the workspace's difference from its starting state, excluding
virtualenvs and caches. Changes to tests and test configuration are dropped; the rest
is applied to a second fresh copy, the private tests are added and the suite runs
sandboxed. `close()` (or leaving the `with` block) deletes workspaces kiln created.

`kiln attempt` runs an agent command in a fresh workspace and stops it from reading
the task's private files, the source repository and the original session's log, so
the attempting agent never sees the reference fix or the demonstration.

## Demonstrations

Every task keeps the trace of the session it came from in
`private/demonstration/` (format `kiln.demo/1`): `events.jsonl` (ordered events with
their original ids and tool-call/result pairing), `messages.json` (the same in chat
format), `edits.jsonl` (each file edit with the content before it) and `meta.json`,
which records truncated tool outputs and any file the session changed without an
edit tool. Hidden reasoning is not in the logs and is not included. Nothing under
`private/` is ever placed in an agent's workspace.

## Past sessions

`kiln traces import` rebuilds a session that wasn't recorded live. The starting state
is the last commit before the session began; the patch is replayed from the session's
own edits (Claude Code logs each file's content before every edit), never taken from
the repository's later state, which may include unrelated changes. The result is:

- **reconstructed**: every edit replays cleanly onto the starting commit;
- **needs review**: a file differed from the starting commit when first edited
  (uncommitted changes), changed between two edits, was altered by the developer, or a
  shell command may have changed files; `task create` requires `--accept-review`;
- **rejected**: the session ran outside the repository, made no edits, or its edits
  change nothing.

Limitation: uncommitted changes to files the session never edited can't be seen in
the log, so the starting state assumes those files matched the starting commit.

## What makes a task trustworthy

**Capture.** Snapshots restore the captured project files exactly, subject to
exclusions: credentials (such as `.env`), caches and anything outside the repository
(installed dependencies, system state) are not captured, so this is not the whole
execution state. Committed source is stored as a plain archive, staged and unstaged
changes as separate binary patches, and untracked files as a tarball, each hashed and
re-checked on restore. Restored workspaces contain a single commit, so no history
before or after the snapshot is reachable from inside a task.

**Instructions.** Task creation refuses an instruction that quotes the reference fix.

**Grading.** Every attempt runs in a freshly restored workspace. The candidate's
edits to tests and to test configuration (`conftest.py`, `pytest.ini`,
`pyproject.toml`, `setup.cfg`, `sitecustomize.py`, …) are dropped, the private
tests are applied, entry-point plugins are disabled, and git runs with hooks and
fsmonitor off. Outcomes come from a guard plugin loaded from outside the
workspace, which records results from raw call information and detects code under
test replacing pytest's reporting. Printed output is never parsed.

**Validation.** Grading is hardened against the eight attacks below, each tested on
every task; passing them does not prove that every possible cheat is blocked.
`kiln task validate` runs the starting state twice and the reference
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

Implemented for Claude Code: live recording, past-session import, task creation with
demonstrations, verification, validation, and the environment interface.

Tested on: a fixture repository (`fixtures/make_toolbox.py`) with nine planted bugs;
its sessions are real Claude Code runs, but they were made specifically to test kiln
(see `fixtures/record_toolbox.sh`), not everyday development sessions. Next: Codex
sessions, Harbor export, and runs on real project history.

Known limits: isolation uses `sandbox-exec` on macOS and `bwrap` on Linux rather than
containers; Python and pytest only; one agent log format so far.
