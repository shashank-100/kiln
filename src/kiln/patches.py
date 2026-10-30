"""Split unified diffs into per-file blocks and classify their paths."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

_TEST_DIRS = {"test", "tests", "testing"}
# Files that change how tests are collected, configured or run. A candidate may
# never change these; its edits to them are dropped before grading.
CONTROL_FILES = (
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py",
    "noxfile.py", "sitecustomize.py", "usercustomize.py", "*.pth", ".pytest.ini",
)  # fmt: skip
_QUOTED = re.compile(r'^"(.*)"$')


def _unquote(raw: str) -> str:
    m = _QUOTED.match(raw)
    if not m:
        return raw
    try:
        return m.group(1).encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return m.group(1)


def _side(line: str, marker: str) -> str | None:
    raw = line[len(marker) :].rstrip("\n").split("\t")[0]
    if raw == "/dev/null":
        return None
    raw = _unquote(raw)
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


@dataclass
class FileDiff:
    old: str | None
    new: str | None
    text: bytes

    @property
    def path(self) -> str:
        return self.new or self.old or ""

    @property
    def paths(self) -> set[str]:
        return {p for p in (self.old, self.new) if p}


def split(patch: bytes) -> list[FileDiff]:
    text = patch.decode("utf-8", "surrogateescape")
    blocks: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") or not blocks:
            blocks.append([])
        blocks[-1].append(line)
    out = []
    for block in blocks:
        if not block or not block[0].startswith("diff --git "):
            continue
        old = new = None
        renamed_from = renamed_to = None
        for line in block:
            if line.startswith("--- "):
                old = _side(line, "--- ")
            elif line.startswith("+++ "):
                new = _side(line, "+++ ")
                break
            elif line.startswith("rename from "):
                renamed_from = _unquote(line[12:].rstrip("\n"))
            elif line.startswith("rename to "):
                renamed_to = _unquote(line[10:].rstrip("\n"))
        if old is None and new is None:
            # Binary or mode-only change: take the paths from the header.
            header = block[0][len("diff --git ") :].rstrip("\n")
            m = re.match(r'^("?a/.*?"?) ("?b/.*"?)$', header)
            if m:
                old, new = _unquote(m.group(1))[2:], _unquote(m.group(2))[2:]
            if any(l.startswith("new file mode") for l in block):
                old = None
            if any(l.startswith("deleted file mode") for l in block):
                new = None
        old, new = renamed_from or old, renamed_to or new
        out.append(FileDiff(old, new, "".join(block).encode("utf-8", "surrogateescape")))
    return out


def join(files: list[FileDiff]) -> bytes:
    return b"".join(f.text for f in files)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        any(p in _TEST_DIRS for p in parts[:-1])
        or fnmatch.fnmatch(name, "test_*.py")
        or fnmatch.fnmatch(name, "*_test.py")
        or name == "conftest.py"
    )


def is_control_path(path: str) -> bool:
    name = path.split("/")[-1]
    return any(fnmatch.fnmatch(name, p) for p in CONTROL_FILES) or path.startswith(".venv/")


def added_lines(patch: bytes) -> list[str]:
    return [
        l[1:] for l in patch.decode(errors="replace").splitlines() if l.startswith("+") and not l.startswith("+++")
    ]


def removed_lines(patch: bytes) -> list[str]:
    return [
        l[1:] for l in patch.decode(errors="replace").splitlines() if l.startswith("-") and not l.startswith("---")
    ]
