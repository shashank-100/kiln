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
