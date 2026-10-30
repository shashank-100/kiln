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


def reconstruct(project: Project, parsed: sessions.Parsed, start_commit: str | None = None) -> Reconstruction:
    root = project.root.resolve()
    cwd = Path(parsed.cwd).resolve() if parsed.cwd else None
    if cwd is None or not (cwd == root or root in cwd.parents):
        return Reconstruction("rejected", reasons=[f"the session ran in {parsed.cwd}, outside {root}"])
    if not parsed.edits:
        return Reconstruction("rejected", reasons=["the session made no file edits with its edit tools"])

    if start_commit is None:
        start_commit = gitutil.out(project.root, "rev-list", "-1", f"--before={parsed.first_ts}", "HEAD")
        if not start_commit:
            return Reconstruction("rejected", reasons=[f"no commit exists before the session began ({parsed.first_ts})"])
    start_commit = gitutil.out(project.root, "rev-parse", "--verify", f"{start_commit}^{{commit}}")
    rec = Reconstruction("reconstructed", start_commit=start_commit)

    contents: dict[str, str | None] = {}
    seen: set[str] = set()
    for e in parsed.edits:
        path = Path(e["file"]).resolve()
        if root not in path.parents:
            rec.reasons.append(f"edit outside the repository ignored: {e['file']}")
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in contents:
            contents[rel] = _show(project.root, start_commit, rel)
        current = contents[rel]
        before = e["original"]
        if e["kind"] == "create" and not before:
            before = None
        if before != current:
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
                rec.reasons.append(f"an edit to {rel} does not apply to the content the log recorded")
                continue
            current = current.replace(e["old"], e["new"]) if e["replace_all"] else current.replace(e["old"], e["new"], 1)
        contents[rel] = current
    rec.files = sorted(contents)

    for e in parsed.events:
        if e.kind == "tool_call" and e.tool == "Bash":
            cmd = str(e.args.get("command", ""))
            if _MUTATING.search(cmd):
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
