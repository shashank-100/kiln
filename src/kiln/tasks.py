"""Package a recorded session as a portable, versioned task.

A task directory is self-contained: it holds the starting snapshot, the
instruction, the private test patch and reference fix, and the verifier
configuration. It can be copied to another machine and validated or graded
without the session or project it came from.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

from kiln import patches, sessions
from kiln.project import Project, dump

SCHEMA = 1
DEMO_FORMAT = "kiln.demo/1"


class TaskError(RuntimeError):
    pass


def _significant(lines: list[str], min_len: int = 20) -> list[str]:
    out = []
    for line in lines:
        s = " ".join(line.split())
        if len(s) >= min_len and sum(c.isalnum() for c in s) >= min_len // 2:
            out.append(s)
    return out


def instruction_leaks(instruction: str, reference: bytes) -> list[str]:
    """Distinctive lines the reference fix adds that the instruction already contains."""
    norm = " ".join(instruction.split())
    removed = set(_significant(patches.removed_lines(reference)))
    return [l for l in _significant(patches.added_lines(reference)) if l not in removed and l in norm]


def content_hash(task_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(task_dir.rglob("*")):
        if p.is_file() and p.name != "validation.json":
            h.update(p.relative_to(task_dir).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def split_session_patch(final: bytes) -> tuple[bytes, bytes]:
    files = patches.split(final)
    tests = [f for f in files if any(patches.is_test_path(p) for p in f.paths)]
    fix = [f for f in files if f not in tests]
    blocked = sorted({p for f in fix for p in f.paths if patches.is_control_path(p)})
    if blocked:
        raise TaskError(
            "the fix changes files a candidate is not allowed to edit "
            f"({', '.join(blocked)}); such tasks are not supported yet"
        )
    return patches.join(fix), patches.join(tests)


def create(
    project: Project,
    session_id: str,
    instruction: str,
    *,
    task_id: str | None = None,
    family: str = "",
    accept_review: bool = False,
) -> Path:
    with project.db() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise TaskError(f"unknown session {session_id}")
    if not row["start_snapshot"] or not row["final_patch"]:
        raise TaskError("this session has no usable starting state and patch (see `kiln session show`)")
    if row["status"] == "imported:needs_review" and not accept_review:
        raise TaskError(
            "this imported session needs review before it can become a task; read "
            f".kiln/sessions/{session_id}/reconstruction.json, then rerun with --accept-review"
        )
    if not instruction.strip():
        raise TaskError("the instruction is empty")

    final = Path(row["final_patch"]).read_bytes()
    fix, tests = split_session_patch(final)
    if not fix.strip():
        raise TaskError("the session changed no source files, so there is no fix to learn")
    if not tests.strip():
        raise TaskError(
            "the session added no tests, so nothing private can check the fix; "
            "add a target test to the session before creating a task"
        )
    leaks = instruction_leaks(instruction, fix)
    if leaks:
        raise TaskError(
            "the instruction quotes the fix; rewrite it to describe the problem only:\n  "
            + "\n  ".join(leaks[:5])
        )

    task_id = task_id or "t-" + hashlib.sha256(session_id.encode()).hexdigest()[:8]
    with project.db() as db:
        version = db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    task_dir = project.dir / "tasks" / task_id / f"v{version}"
    task_dir.mkdir(parents=True)
    shutil.copytree(project.dir / "snapshots" / row["start_snapshot"], task_dir / "start")
    (task_dir / "private").mkdir()
    (task_dir / "private" / "reference.patch").write_bytes(fix)
    (task_dir / "private" / "test.patch").write_bytes(tests)
    (task_dir / "instruction.md").write_text(instruction.strip() + "\n", encoding="utf-8")
    demo = write_demonstration(project, session_id, final, task_dir / "private" / "demonstration")

    cfg = project.config()
    start_meta = json.loads((task_dir / "start" / "meta.json").read_text(encoding="utf-8"))
    manifest = {
        "schema": SCHEMA,
        "id": task_id,
        "version": version,
        "family": family or task_id,
        "created": dt.datetime.now(dt.UTC).isoformat(),
        "repository": {"head": start_meta["head"], "branch": start_meta["branch"]},
        "start": {"snapshot": start_meta["id"], "state_hash": start_meta["state_hash"]},
        "environment": {
            "runtime": start_meta["runtime"],
            "lockfiles": start_meta["lockfiles"],
            "network": "required for setup only",
        },
        "execution": {"tools": ["read", "search", "edit", "shell"], "max_steps": 50,
                      "command_timeout": 120, "overall_timeout": 1800},  # fmt: skip
        "verification": {
            **cfg["verify"],
            "test_files": sorted({f.path for f in patches.split(tests)}),
        },
        "reference": {"files": sorted({f.path for f in patches.split(fix)})},
        "demonstration": {k: demo[k] for k in ("format", "available", "events", "completeness",
                                                "truncated_results", "changed_outside_edit_tools") if k in demo},
        "provenance": {
            "method": "recorded-session",
            "session": session_id,
            "agent": row["agent"],
            "parser": sessions.PARSER,
            "review": "instruction reviewed at creation"
            + ("; reconstruction accepted after review" if accept_review else ""),
            "start_state": "captured live" if not str(row["status"]).startswith("imported")
            else "rebuilt from a commit and the session's own edits",
        },
    }
    (task_dir / "manifest.json").write_text(dump(manifest), encoding="utf-8")
    with project.db() as db:
        db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, 'created', ?, ?, ?)",
            (task_id, version, session_id, manifest["created"], str(task_dir), content_hash(task_dir)),
        )
    return task_dir


def _messages(events: list[dict]) -> list[dict]:
    """Chat-format view of the trace: one message per event, ids and order preserved."""
    out = []
    for e in events:
        if e["kind"] == "message":
            out.append({"role": e["role"], "content": e["text"], "event": e["id"]})
        elif e["kind"] == "tool_call":
            out.append({"role": "assistant", "content": "", "event": e["id"],
                        "tool_calls": [{"id": e["call_id"], "name": e["tool"], "arguments": e["args"]}]})
        else:
            out.append({"role": "tool", "tool_call_id": e["call_id"], "name": e["tool"], "content": e["text"],
                        "is_error": e["is_error"], "truncated": e.get("truncated", False), "event": e["id"]})
    return out  # fmt: skip


def write_demonstration(project: Project, session_id: str, final_patch: bytes, out: Path) -> dict:
    """Bundle the session's own trace with the task, outside anything an agent sees."""
    log = project.dir / "sessions" / session_id / "log"
    if not (log / "events.jsonl").exists():
        return {"format": DEMO_FORMAT, "available": False, "reason": "no agent log was linked to this session"}
    out.mkdir(parents=True)
    events = list(sessions.read_events(log))
    edits_path = log / "edits.jsonl"
    edits = [json.loads(l) for l in edits_path.read_text(encoding="utf-8").splitlines()] if edits_path.exists() else []
    shutil.copy(log / "events.jsonl", out / "events.jsonl")
    if edits_path.exists():
        shutil.copy(edits_path, out / "edits.jsonl")
    if (log / "blobs").exists():
        shutil.copytree(log / "blobs", out / "blobs")
    (out / "messages.json").write_text(json.dumps(_messages(events), indent=1, ensure_ascii=False), encoding="utf-8")

    root = project.root.resolve()
    edited = set()
    for e in edits:
        p = Path(e["file"]).resolve()
        if root in p.parents:
            edited.add(p.relative_to(root).as_posix())
    changed = {f.path for f in patches.split(final_patch)}
    session_meta = json.loads((log / "session.json").read_text(encoding="utf-8"))
    meta = {
        "format": DEMO_FORMAT,
        "available": True,
        "event_format": session_meta.get("event_format", ""),
        "parser": session_meta.get("parser", ""),
        "agent_session_id": session_meta.get("session_id", ""),
        "events": len(events),
        "completeness": session_meta.get("completeness", ""),
        "truncated_results": sum(1 for e in events if e.get("truncated")),
        "sidechain_events": sum(1 for e in events if e.get("sidechain")),
        "changed_outside_edit_tools": sorted(changed - edited),
        "note": "Hidden reasoning is not part of the log and is not included.",
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def resolve(project: Project | None, ref: str) -> Path:
    """A task directory from a path, `<id>` (latest version) or `<id>@<version>`."""
    p = Path(ref)
    if (p / "manifest.json").exists():
        return p
    if project is None:
        raise TaskError(f"{ref} is not a task directory")
    task_id, _, ver = ref.partition("@")
    with project.db() as db:
        row = db.execute(
            "SELECT path FROM tasks WHERE id=? AND (?='' OR version=?) ORDER BY version DESC LIMIT 1",
            (task_id, ver, ver or 0),
        ).fetchone()
    if row is None:
        raise TaskError(f"unknown task {ref}")
    return Path(row["path"])
