"""The environment interface: reset a task, let an agent attempt it, get a reward.

    with Env("path/to/task") as env:
        workspace = env.reset()          # fresh copy of the starting state, environment set up
        ...                              # an agent edits files in `workspace`
        result = env.grade()             # Result(status, reward, reason, diagnostics)

How an attempt is graded: the candidate patch is the difference between the
workspace and its starting state (virtualenvs and caches excluded). Changes to
test files and test configuration are dropped; the rest is applied to a second,
fresh copy of the starting state, the task's private tests are added, and the
suite runs sandboxed. The workspace itself is never trusted or reused.

Status is `passed` (reward 1.0), `failed` (reward 0.0) or `ungraded` (reward
None). Infrastructure problems (setup failure, a modified task, a missing
sandbox) are always ungraded, never counted as a failed attempt.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiln import gitutil, sandbox, snapshot, verify

_EXCLUDE = (".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


class EnvError(RuntimeError):
    """The environment itself is unusable (not the agent's fault)."""


@dataclass
class Result:
    status: str  # passed | failed | ungraded
    reward: float | None
    reason: str
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Env:
    def __init__(self, task_dir: str | Path):
        self.task_dir = Path(task_dir)
        self.manifest = json.loads((self.task_dir / "manifest.json").read_text(encoding="utf-8"))
        vpath = self.task_dir / "validation.json"
        self.validation = json.loads(vpath.read_text(encoding="utf-8")) if vpath.exists() else {}
        self.workspace: Path | None = None
        self._owned: Path | None = None

    @property
    def id(self) -> str:
        return f"{self.manifest['id']}@{self.manifest['version']}"

    @property
    def instruction(self) -> str:
        return (self.task_dir / "instruction.md").read_text(encoding="utf-8")

    def reset(self, path: str | Path | None = None) -> Path:
        """A fresh workspace at `path` (must be empty or absent), or in a temp dir kiln cleans up."""
        if self.validation.get("status") != "valid":
            raise EnvError(f"task {self.id} is not validated (status: {self.validation.get('status', 'none')})")
        self.close()
        if path is None:
            self._owned = Path(tempfile.mkdtemp(prefix="kiln-env-"))
            ws = self._owned / "workspace"
        else:
            ws = Path(path)
        try:
            snapshot.restore(self.task_dir / "start", ws)
        except (snapshot.RestoreError, snapshot.CaptureError, gitutil.GitError) as e:
            raise EnvError(f"restore failed: {e}") from e
        home = Path(tempfile.mkdtemp(prefix="kiln-env-home-"))
        for cmd in self.manifest["verification"].get("setup", []):
            r = subprocess.run(cmd, cwd=ws, env=verify._env(home), capture_output=True, text=True)
            if r.returncode != 0:
                raise EnvError(f"setup failed: {' '.join(cmd)}: {r.stderr[-500:]}")
        shutil.rmtree(home, ignore_errors=True)
        self.workspace = ws
        return ws

    def candidate_patch(self, workspace: str | Path | None = None) -> bytes:
        ws = Path(workspace) if workspace else self.workspace
        if ws is None:
            raise EnvError("no workspace: call reset() first")
        gitutil.run(ws, "add", "-A", "-f", "--", ".",
                    *[f":(exclude,glob)**/{d}/**" for d in _EXCLUDE], ":(exclude,glob)**/*.py[co]")  # fmt: skip
        return gitutil.run(ws, "diff", "--cached", "--binary", "--no-ext-diff", "start").stdout

    def grade(self, patch: bytes | None = None, workspace: str | Path | None = None) -> Result:
        try:
            if patch is None:
                patch = self.candidate_patch(workspace)
            if sandbox.available() is None:
                return Result("ungraded", None, "no_sandbox", {"detail": "install bwrap (Linux) to grade safely"})
            o = verify.grade(self.task_dir, patch)
        except (EnvError, gitutil.GitError, OSError) as e:
            return Result("ungraded", None, "infrastructure_error", {"detail": str(e)})
        diagnostics = {
            "tests": o.tests,
            "targets": self.validation.get("targets", []),
            "dropped_files": o.dropped,
            "tampering": o.tampered,
            "seconds": round(o.seconds, 2),
            "patch_bytes": len(patch),
            "log_tail": o.log[-2000:],
        }
        return Result(o.status, o.reward, o.reason, diagnostics)

    def close(self) -> None:
        if self._owned is not None:
            shutil.rmtree(self._owned, ignore_errors=True)
        self._owned = None
        self.workspace = None

    def __enter__(self) -> Env:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _claude_logs_for(path: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(path))


def attempt(
    task_dir: Path,
    command: list[str],
    *,
    hidden: list[Path],
    timeout: int = 900,
    out_dir: Path | None = None,
) -> dict:
    """Reset, run an agent command in the workspace, grade it. The agent can't read `hidden`.

    `{instruction}` in the command is replaced by the task instruction, which is
    also passed as $KILN_INSTRUCTION. Nothing from the task's private files is
    placed in the workspace.
    """
    import os

    env_obj = Env(task_dir)
    record: dict = {"id": uuid.uuid4().hex[:12], "task": env_obj.id, "command": command}
    t0 = time.monotonic()
    with env_obj:
        try:
            ws = env_obj.reset()
        except EnvError as e:
            record.update(result=Result("ungraded", None, "reset_failed", {"detail": str(e)}).to_dict())
            return record
        argv = [a.replace("{instruction}", env_obj.instruction.strip()) for a in command]
        run_env = {**os.environ, "KILN_INSTRUCTION": env_obj.instruction.strip()}
        hide = [*hidden, task_dir.resolve()]
        started = time.time() - 1
        try:
            proc = subprocess.run(sandbox.hide(argv, hide), cwd=ws, env=run_env, capture_output=True,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)  # fmt: skip
            record.update(agent_exit=proc.returncode, agent_output_tail=proc.stdout[-3000:] + proc.stderr[-1000:])
            termination = "exited"
            if proc.returncode in (126, 127) or (
                proc.returncode in range(64, 79) and proc.stderr.lstrip().startswith(("sandbox-exec:", "bwrap:"))
            ):
                # The agent never ran: that says nothing about the task or the agent's ability.
                record.update(termination="launch_failed", result=Result(
                    "ungraded", None, "agent_launch_failed", {"detail": proc.stderr[-500:]}).to_dict())
                return record
        except subprocess.TimeoutExpired:
            termination = "agent_timeout"
            record["agent_exit"] = None
        except (OSError, RuntimeError) as e:
            record.update(result=Result("ungraded", None, "agent_launch_failed", {"detail": str(e)}).to_dict())
            return record
        record["termination"] = termination
        result = env_obj.grade()
        record["result"] = result.to_dict()
        record["patch"] = env_obj.candidate_patch().decode("utf-8", "replace")
        logs = _claude_logs_for(ws.resolve())
        record["agent_logs"] = [str(p) for p in sorted(logs.glob("*.jsonl")) if p.stat().st_mtime >= started] if logs.is_dir() else []
    record["seconds"] = round(time.monotonic() - t0, 1)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{record['id']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
