"""Record an agent session: snapshot before, run the agent, snapshot after, link its log."""

from __future__ import annotations

import datetime as dt
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from kiln import sessions, snapshot
from kiln.project import Project


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _session_id() -> str:
    return "ses-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")


def _import_logs(project: Project, sid: str, logs: list[Path]) -> tuple[int, str]:
    if not logs:
        return 0, "no-agent-log"
    total, worst = 0, "complete"
    for i, log in enumerate(logs):
        parsed = sessions.parse_claude_code(log)
        sessions.write(parsed, project.dir / "sessions" / sid / ("log" if i == 0 else f"log{i}"))
        total += len(parsed.events)
        if parsed.completeness != "complete":
            worst = parsed.completeness
    return total, worst


def record(project: Project, command: list[str], *, agent: str = "claude-code") -> str:
    sid = _session_id()
    start = snapshot.capture(project, f"{sid}:start")
    started_at = time.time() - 1
    with project.db() as db:
        db.execute(
            "INSERT INTO sessions (id, agent, source, started, status, start_snapshot) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (sid, agent, " ".join(command), _now(), start),
        )

    status, code = "completed", None
    try:
        proc = subprocess.Popen(command, cwd=project.root)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            status = "interrupted"
            proc.send_signal(signal.SIGINT)
            try:
                code = proc.wait(timeout=30)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
                code = proc.wait()
    except FileNotFoundError:
        status, code = "failed", 127
    if status == "completed" and code not in (0, None):
        status = "agent-error"

    end = snapshot.capture(project, f"{sid}:end")
    snaps = project.dir / "snapshots"
    with tempfile.TemporaryDirectory(prefix="kiln-diff-") as tmp:
        patch = snapshot.diff_states(snaps / start, snaps / end, Path(tmp))
    out = project.dir / "sessions" / sid
    out.mkdir(parents=True, exist_ok=True)
    (out / "final.patch").write_bytes(patch)

    logs = sessions.claude_code_logs(project.root, started_at) if agent == "claude-code" else []
    for i, log in enumerate(logs):
        shutil.copy2(log, out / f"raw{i}.jsonl")
    n_events, completeness = _import_logs(project, sid, logs)
    if status != "completed":
        completeness = f"partial ({status})"

    with project.db() as db:
        db.execute(
            "UPDATE sessions SET ended=?, status=?, exit_code=?, end_snapshot=?, final_patch=?, "
            "events=?, completeness=? WHERE id=?",
            (_now(), status, code, end, str(out / "final.patch"), n_events, completeness, sid),
        )
    return sid


def import_trace(
    project: Project, path: Path, start_commit: str | None = None, session_root: Path | None = None
) -> str:
    """Import an existing log; rebuild its starting state and patch when the log allows it."""
    import json

    from kiln import reconstruct

    sid = _session_id()
    out = project.dir / "sessions" / sid
    parsed = sessions.parse_claude_code(path)
    sessions.write(parsed, out / "log")
    shutil.copy2(path, out / "raw0.jsonl")
    rec = reconstruct.reconstruct(project, parsed, start_commit, session_root)
    (out / "reconstruction.json").write_text(json.dumps(rec.summary(), indent=2), encoding="utf-8")
    final = None
    if rec.status in ("reconstructed", "needs_review"):
        final = out / "final.patch"
        final.write_bytes(rec.patch)
    with project.db() as db:
        db.execute(
            "INSERT INTO sessions (id, agent, source, started, ended, status, start_snapshot, final_patch, "
            "events, completeness) VALUES (?, 'claude-code', ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, str(path), parsed.first_ts, parsed.last_ts, f"imported:{rec.status}",
             rec.start_snapshot or None, str(final) if final else None, len(parsed.events),
             parsed.completeness),
        )  # fmt: skip
    return sid
