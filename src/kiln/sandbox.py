"""Run untrusted commands (candidate code, model tool calls) with no network and
writes confined to their workspace.

macOS uses the built-in `sandbox-exec`. On Linux, `bwrap` is used when installed.
Without either, commands run unconfined and `available()` says so, so callers can
refuse or warn.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def available() -> str | None:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def _sb_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def hide(cmd: list[str], hidden: list[Path]) -> list[str]:
    """Let a command use the network and write freely, but not read `hidden` paths.

    Used for attempting agents: they need their model API, but must not see the
    task's answer, the source repository or the original session log.
    """
    kind = available()
    paths = [Path(os.path.realpath(p)) for p in hidden if os.path.exists(p)]
    if not paths:
        return cmd
    if kind == "sandbox-exec":
        deny = " ".join(f"(subpath {_sb_quote(p)})" for p in paths)
        return ["sandbox-exec", "-p", f"(version 1)(allow default)(deny file-read* file-write* {deny})", *cmd]
    if kind == "bwrap":
        args = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for p in paths:
            args += ["--tmpfs", str(p)]
        return [*args, *cmd]
    raise RuntimeError("no sandbox available to hide the task's answer from the attempting agent")


def wrap(cmd: list[str], writable: list[Path]) -> list[str]:
    kind = available()
    paths = [Path(os.path.realpath(p)) for p in writable]
    if kind == "sandbox-exec":
        allow = " ".join(f"(subpath {_sb_quote(p)})" for p in paths)
        profile = (
            "(version 1)(allow default)"
            "(deny network*)"
            "(allow network* (remote unix-socket))"
            "(deny file-write*)"
            f'(allow file-write* {allow} (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))'
        )
        return ["sandbox-exec", "-p", profile, *cmd]
    if kind == "bwrap":
        args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net"]
        for p in paths:
            args += ["--bind", str(p), str(p)]
        return [*args, *cmd]
    return cmd
