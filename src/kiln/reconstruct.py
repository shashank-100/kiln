"""Rebuild a past session's starting state and its own patch from its log.

The patch comes only from the session's recorded edits (each Edit/Write result
carries the file's content before the edit), replayed onto the commit the
repository was at when the session began. Later repository state is never used,
because it may contain unrelated changes.

Every replay step is checked against the log. A file whose recorded content at
first edit differs from the starting commit had uncommitted (or earlier) changes;
a file that changed between two edits was modified outside the edit tools. Both
send the session to review. Uncommitted changes to files the session never
edited cannot be seen in the log at all, which is reported as a limitation.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiln import gitutil, sessions, snapshot
from kiln.project import Project

# Shell commands that can change files without going through the edit tools.
_MUTATING = re.compile(
    r"(?<![0-9&])>>?\s*(?!/dev/)[\w./~-]"  # redirect into a file
    r"|\bsed\s+-i|\bperl\s+-[a-z]*i|\btee\b|\bmv\b|\bcp\b|\brm\b|\btouch\b|\bpatch\b"
    r"|\bgit\s+(?:apply|checkout|restore|reset|stash|pull|merge|rebase|cherry-pick|am)\b"
)
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_REDIRECT = re.compile(r"(?<![0-9&<])>>?\s*([^\s;&|)]+)")


def _may_mutate(cmd: str, root: Path) -> bool:
    """Heuristic: could this shell command change files in the repository?"""
    bare = _QUOTED.sub("''", cmd)  # quoted text is data (grep patterns, awk programs), not commands
    for target in _REDIRECT.findall(bare):
        t = os.path.expanduser(target)
        if t.startswith("/dev/"):
            continue
        if not os.path.isabs(t) or Path(t) == root or root in Path(t).parents:
            return True
    without_redirects = _REDIRECT.sub(" ", bare)
    return bool(_MUTATING.search(without_redirects.replace(">", " ")))


_COMMIT_OUT = re.compile(r"^\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,40})\]", re.MULTILINE)
LIMITATION = (
    "Uncommitted changes to files the session did not edit cannot be detected from the log; "
    "the starting state assumes those files matched the starting commit."
)


@dataclass
class Reconstruction:
    status: str  # reconstructed | needs_review | rejected
    start_commit: str = ""
    start_snapshot: str = ""
    files: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    linked_commits: list[str] = field(default_factory=list)
    limitation: str = LIMITATION
    patch: bytes = b""

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("patch")
        d["patch_bytes"] = len(self.patch)
        return d


def _show(repo: Path, commit: str, rel: str) -> str | None:
    r = gitutil.run(repo, "show", f"{commit}:{rel}", check=False)
    return r.stdout.decode("utf-8", "surrogateescape") if r.returncode == 0 else None


def reconstruct(
    project: Project,
    parsed: sessions.Parsed,
    start_commit: str | None = None,
    session_root: Path | None = None,
) -> Reconstruction:
    """`session_root` is where this repository lived when the session ran (default: here)."""
    root = Path(os.path.abspath(session_root or project.root))
    real_root = root.resolve()

    def inside(p: str) -> bool:
        a = Path(os.path.abspath(p))
        return root in a.parents or real_root in Path(a).resolve().parents

    ours = [e for e in parsed.edits if inside(e["file"])]
    if not ours:
        where = "anywhere" if not parsed.edits else f"inside {root}"
        return Reconstruction("rejected", reasons=[f"the session made no edits {where} with its edit tools"])
    notes = []
    others = sorted({e["file"] for e in parsed.edits if not inside(e["file"])})
    if others:
        notes.append(f"{len(others)} file(s) edited outside this repository were set aside")
    cwd = Path(parsed.cwd) if parsed.cwd else None
    if cwd is None or not (cwd == root or root in cwd.parents):
        notes.append(f"the session started in {parsed.cwd}, not in the repository")

    first_edit_ts = next((e["ts"] for e in ours if e.get("ts")), parsed.first_ts)
    if start_commit is None:
        start_commit = gitutil.out(project.root, "rev-list", "-1", f"--before={first_edit_ts}", "HEAD")
        if not start_commit:
            return Reconstruction("rejected", reasons=[f"no commit exists before the session's first edit ({first_edit_ts})"])
        notes.append(f"starting commit: last commit before the first edit in this repository ({first_edit_ts})")
    start_commit = gitutil.out(project.root, "rev-parse", "--verify", f"{start_commit}^{{commit}}")
    rec = Reconstruction("reconstructed", start_commit=start_commit, notes=notes)

    contents: dict[str, str | None] = {}
    seen: set[str] = set()
    unrecorded = 0
    for e in ours:
        a = Path(os.path.abspath(e["file"]))
        rel = (a.relative_to(root) if root in a.parents else a.resolve().relative_to(real_root)).as_posix()
        if rel not in contents:
            contents[rel] = _show(project.root, start_commit, rel)
        current = contents[rel]
        before = e["original"]
        if e["kind"] == "create" and not before:
            before = None
        if before is None and e["kind"] == "edit":
            # The log omitted the file's content before this edit. Missing evidence is not
            # contradicting evidence: rely on the edit's own text matching the replay instead.
            unrecorded += 1
        elif before != current:
            if rel not in seen:
                rec.reasons.append(f"{rel} differed from the starting commit when the session first edited it "
                                   "(uncommitted or earlier changes)")  # fmt: skip
            else:
                rec.reasons.append(f"{rel} changed between two of the session's edits (outside its edit tools)")
            current = before  # the log is the authority on what the agent saw
        seen.add(rel)
        if e["user_modified"]:
            rec.reasons.append(f"the developer modified the agent's proposed edit to {rel}")
        if e["kind"] in ("create", "update"):
            current = e["content"] or ""
        else:
            if current is None or e["old"] not in current:
                rec.reasons.append(f"an edit to {rel} does not apply to the replayed content")
                continue
            if not e["replace_all"] and current.count(e["old"]) != 1:
                rec.reasons.append(f"an edit to {rel} matches {current.count(e['old'])} places in the replayed content")
                continue
            current = current.replace(e["old"], e["new"]) if e["replace_all"] else current.replace(e["old"], e["new"], 1)
        contents[rel] = current
    rec.files = sorted(contents)
    if unrecorded:
        rec.notes.append(f"the log omitted before-content for {unrecorded} edit(s); those were checked by "
                         "matching each edit's text against the replay instead")  # fmt: skip

    repo_markers = {str(root), str(real_root), root.name}
    for e in parsed.events:
        if e.kind == "tool_call" and e.tool == "Bash":
            cmd = str(e.args.get("command", ""))
            # In a multi-project session, only commands that may touch this repository matter.
            relevant = any(m in cmd for m in repo_markers) or (cwd is not None and (cwd == root or root in cwd.parents))
            if relevant and _may_mutate(cmd, root):
                rec.reasons.append(f"shell command may have changed files outside the edit tools: {cmd[:120]}")
        if e.kind == "tool_result" and e.tool == "Bash":
            rec.linked_commits += _COMMIT_OUT.findall(e.text)

    with tempfile.TemporaryDirectory(prefix="kiln-recon-") as tmp:
        rec.start_snapshot = snapshot.capture_commit(project, start_commit, "reconstructed:start")
        snap = project.dir / "snapshots" / rec.start_snapshot
        ws = Path(tmp) / "ws"
        snapshot.restore(snap, ws)
        for rel, body in contents.items():
            target = ws / rel
            if body is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8", errors="surrogateescape")
        gitutil.run(ws, "add", "-A", "-f")
        rec.patch = gitutil.run(ws, "diff", "--cached", "--binary", "--no-ext-diff", "start").stdout

    if not rec.patch.strip():
        rec.status = "rejected"
        rec.reasons.append("replaying the session's edits leaves the starting commit unchanged")
    elif rec.reasons:
        rec.status = "needs_review"
    return rec
