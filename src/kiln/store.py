"""Retention, deletion and export of what kiln keeps under `.kiln/`.

Tasks are self-contained copies, so removing sessions or snapshots never breaks
a task; task provenance stays in its manifest.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tarfile
from pathlib import Path

from kiln.project import Project


def delete_session(project: Project, sid: str) -> list[str]:
    removed = []
    with project.db() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        db.execute("UPDATE tasks SET session_id=NULL WHERE session_id=?", (sid,))
        db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        for snap in (row["start_snapshot"], row["end_snapshot"]):
            if snap:
                db.execute("DELETE FROM snapshots WHERE id=?", (snap,))
                path = project.dir / "snapshots" / snap
                if path.exists():
                    shutil.rmtree(path)
                    removed.append(f"snapshots/{snap}")
    path = project.dir / "sessions" / sid
    if path.exists():
        shutil.rmtree(path)
        removed.append(f"sessions/{sid}")
    return removed


def delete_task(project: Project, task_id: str, version: int | None = None) -> list[str]:
    removed = []
    with project.db() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE id=? AND (? IS NULL OR version=?)", (task_id, version, version)
        ).fetchall()
        if not rows:
            raise KeyError(task_id)
        for r in rows:
            shutil.rmtree(r["path"], ignore_errors=True)
            db.execute("DELETE FROM tasks WHERE id=? AND version=?", (r["id"], r["version"]))
            db.execute("DELETE FROM runs WHERE task_id=? AND task_version=?", (r["id"], r["version"]))
            removed.append(f"tasks/{r['id']}/v{r['version']}")
    parent = project.dir / "tasks" / task_id
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return removed


def gc(project: Project, keep_days: int, dry_run: bool = False) -> list[str]:
    """Delete sessions older than `keep_days` that no task came from, and orphaned snapshots."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=keep_days)).isoformat()
    with project.db() as db:
        old = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM sessions WHERE COALESCE(ended, started, '') < ? "
                "AND id NOT IN (SELECT session_id FROM tasks WHERE session_id IS NOT NULL)",
                (cutoff,),
            )
        ]
        referenced = {
            s for r in db.execute("SELECT start_snapshot, end_snapshot FROM sessions") for s in r if s
        }
    removed = []
    for sid in old:
        removed += [f"sessions/{sid}"] if dry_run else delete_session(project, sid)
    snaps = project.dir / "snapshots"
    for path in sorted(snaps.iterdir()) if snaps.exists() else []:
        if path.name not in referenced and path.name not in {r.split("/")[-1] for r in removed}:
            removed.append(f"snapshots/{path.name}")
            if not dry_run:
                shutil.rmtree(path)
                with project.db() as db:
                    db.execute("DELETE FROM snapshots WHERE id=?", (path.name,))
    return removed


def export_task(task_dir: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(task_dir, arcname=task_dir.parent.name + "-" + task_dir.name)
    return out
