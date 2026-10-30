"""Run git without executing anything the repository configures.

Hooks, fsmonitor, external diff drivers and the user's global config are all
disabled, so reading a developer's repository (or a candidate's workspace)
never runs code from it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_SAFE = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "commit.gpgsign=false",
    "-c", "user.name=kiln",
    "-c", "user.email=kiln@localhost",
]  # fmt: skip
_HOME = tempfile.mkdtemp(prefix="kiln-githome-")


def env() -> dict[str, str]:
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    return {
        **keep,
        "HOME": _HOME,
        "XDG_CONFIG_HOME": _HOME,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }


class GitError(RuntimeError):
    pass


def run(
    repo: Path, *args: str, input: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        ["git", *_SAFE, "-C", str(repo), *args],
        input=input,
        capture_output=True,
        env=env(),
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def out(repo: Path, *args: str) -> str:
    return run(repo, *args).stdout.decode().strip()


def toplevel(start: Path) -> Path:
    try:
        return Path(out(start, "rev-parse", "--show-toplevel"))
    except GitError as e:
        raise SystemExit(f"{start} is not inside a git repository") from e


def git_dir(repo: Path) -> str:
    return str((repo / out(repo, "rev-parse", "--git-dir")).resolve())
