"""kiln: turn coding-agent sessions into verified, replayable RL tasks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kiln import project as proj
from kiln import record, sessions, snapshot, store, tasks, verify


def _p() -> proj.Project:
    return proj.find(Path.cwd())


def cmd_init(a) -> int:
    p = proj.init(Path.cwd())
    print(f"initialised kiln in {p.dir}")
    print(f"edit {p.config_path} to set how tasks are set up and tested")
    return 0


def cmd_doctor(a) -> int:
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'ok ' if passed else 'FAIL'}  {name}{'  ' + detail if detail else ''}")

    git = shutil.which("git")
    check("git", bool(git), subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip() if git else "not found")
    try:
        p = _p()
    except SystemExit as e:
        check("project", False, str(e))
        return 1
    check("project", True, str(p.root))
    free = shutil.disk_usage(p.root).free / 2**30
    check("disk space", free > 2, f"{free:.1f} GiB free")
    try:
        cfg = p.config()
        check("config", True, str(p.config_path))
    except Exception as e:  # noqa: BLE001 - report any parse problem
        check("config", False, str(e))
        return 1
    for cmd in [*cfg["verify"].get("setup", []), cfg["verify"]["pytest"]]:
        exe = cmd[0]
        found = exe.startswith(".") or shutil.which(exe) is not None
        check(f"executable {exe}", found, "created by setup" if exe.startswith(".") else (shutil.which(exe) or "not found"))
    logs = Path.home() / ".claude" / "projects"
    check("claude code logs", logs.is_dir() and os.access(logs, os.R_OK), str(logs))
    return 0 if ok else 1


def cmd_snapshot(a) -> int:
    p = _p()
    sid = snapshot.capture(p, a.label)
    print(sid)
    if a.restore:
        snapshot.restore(p.dir / "snapshots" / sid, Path(a.restore))
        print(f"restored and verified in {a.restore}")
    return 0


def cmd_record(a) -> int:
    if not a.command:
        print("usage: kiln record -- <agent command>", file=sys.stderr)
        return 2
    p = _p()
    sid = record.record(p, a.command)
    _show_session(p, sid)
    return 0


def cmd_import(a) -> int:
    p = _p()
    sid = record.import_trace(p, Path(a.path))
    _show_session(p, sid)
    return 0


def _show_session(p: proj.Project, sid: str) -> None:
    with p.db() as db:
        r = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    patch = Path(r["final_patch"]).read_bytes() if r["final_patch"] else b""
    print(f"session   {r['id']}  ({r['status']}, exit {r['exit_code']})")
    print(f"events    {r['events']}  [{r['completeness']}]")
    if r["start_snapshot"]:
        print(f"start     {r['start_snapshot']}")
        print(f"end       {r['end_snapshot']}")
        files = [f.path for f in __import__('kiln.patches', fromlist=['split']).split(patch)]
        print(f"changed   {len(files)} file(s): {', '.join(files[:8])}")


def cmd_sessions(a) -> int:
    p = _p()
    with p.db() as db:
        rows = db.execute("SELECT * FROM sessions ORDER BY started DESC, id DESC").fetchall()
    for r in rows:
        print(f"{r['id']}  {r['status']:<12} events={r['events']:<4} {r['completeness']}  {r['source'] or ''}")
    return 0


def cmd_task_create(a) -> int:
    p = _p()
    if a.instruction_file:
        instruction = Path(a.instruction_file).read_text(encoding="utf-8")
    else:
        draft = sessions.first_request(p.dir / "sessions" / a.session / "log")
        print("Draft instruction from the session's opening request:\n")
        print("  " + draft.replace("\n", "\n  "))
        print("\nReview it, remove any hint of the solution, save it to a file and rerun with")
        print(f"  kiln task create --session {a.session} --instruction-file <file>")
        return 2
    try:
        d = tasks.create(p, a.session, instruction, task_id=a.id)
    except tasks.TaskError as e:
        print(f"cannot create task: {e}", file=sys.stderr)
        return 1
    m = json.loads((d / "manifest.json").read_text())
    print(f"created {m['id']} v{m['version']} at {d}")
    print(f"  fix touches   {', '.join(m['reference']['files'])}")
    print(f"  private tests {', '.join(m['verification']['test_files'])}")
    print(f"next: kiln task validate {m['id']}")
    return 0


def _task_dir(ref: str) -> Path:
    try:
        p = _p()
    except SystemExit:
        p = None
    return tasks.resolve(p, ref)


def cmd_task_validate(a) -> int:
    d = _task_dir(a.task)
    v = verify.validate(d, repeats=a.repeats, on_step=lambda m: print(f"  - {m}", flush=True))
    print()
    print(f"status       {v['status']}: {v['reason']}")
    for key in ("targets", "regressions", "preexisting_failures", "flaky"):
        if key in v:
            print(f"{key:<12} {len(v[key])}  {', '.join(v[key][:4])}")
    c = v["checks"]
    if "graded_reference" in c:
        print(f"reference    reward {c['graded_reference']['reward']}  ({c['graded_reference']['reason']})")
        print(f"empty patch  reward {c['graded_empty']['reward']}  ({c['graded_empty']['reason']})")
    for name, r in c.get("attacks", {}).items():
        mark = "blocked" if r["reward"] == 0.0 else "ACCEPTED"
        how = r["reason"] + (f", dropped {','.join(r['dropped'])}" if r["dropped"] else "")
        print(f"attack       {mark:<8} {name:<26} {how}")
    _record_status(d, v["status"])
    return 0 if v["status"] == "valid" else 1


def _record_status(task_dir: Path, status: str) -> None:
    try:
        p = _p()
    except SystemExit:
        return
    m = json.loads((task_dir / "manifest.json").read_text())
    with p.db() as db:
        db.execute("UPDATE tasks SET status=?, content_hash=? WHERE id=? AND version=?",
                   (status, tasks.content_hash(task_dir), m["id"], m["version"]))  # fmt: skip


def cmd_tasks(a) -> int:
    p = _p()
    with p.db() as db:
        for r in db.execute("SELECT * FROM tasks ORDER BY created DESC"):
            print(f"{r['id']}@{r['version']}  {r['status']:<11} session={r['session_id']}  {r['path']}")
    return 0


def cmd_grade(a) -> int:
    d = _task_dir(a.task)
    patch = Path(a.patch).read_bytes() if a.patch != "-" else sys.stdin.buffer.read()
    o = verify.grade(d, patch)
    print(verify.describe(o))
    if a.verbose:
        for t, s in sorted(o.tests.items()):
            print(f"  {s:<8} {t}")
    return 0 if o.status != "ungraded" else 2


def cmd_session_delete(a) -> int:
    for r in store.delete_session(_p(), a.session):
        print(f"removed {r}")
    return 0


def cmd_task_delete(a) -> int:
    tid, _, ver = a.task.partition("@")
    for r in store.delete_task(_p(), tid, int(ver) if ver else None):
        print(f"removed {r}")
    return 0


def cmd_task_export(a) -> int:
    out = store.export_task(_task_dir(a.task), Path(a.out))
    print(f"wrote {out}")
    return 0


def cmd_gc(a) -> int:
    removed = store.gc(_p(), a.keep_days, dry_run=a.dry_run)
    verb = "would remove" if a.dry_run else "removed"
    for r in removed:
        print(f"{verb} {r}")
    print(f"{len(removed)} item(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kiln", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="register this repository").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check prerequisites").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("snapshot", help="capture the working tree (optionally restore it elsewhere)")
    s.add_argument("--label", default="manual")
    s.add_argument("--restore", metavar="DIR")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("record", help="record an agent session: kiln record -- <command>")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("traces", help="work with existing agent logs")
    ts = s.add_subparsers(dest="sub", required=True)
    t = ts.add_parser("import", help="import a Claude Code session log")
    t.add_argument("path")
    t.set_defaults(fn=cmd_import)

    sub.add_parser("sessions", help="list sessions").set_defaults(fn=cmd_sessions)
    s = sub.add_parser("session", help="manage one session")
    ts = s.add_subparsers(dest="sub", required=True)
    t = ts.add_parser("delete", help="delete a session, its log copy and its snapshots")
    t.add_argument("session")
    t.set_defaults(fn=cmd_session_delete)

    s = sub.add_parser("gc", help="apply retention: drop old sessions with no task, and orphaned snapshots")
    s.add_argument("--keep-days", type=int, default=30)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("task", help="create and validate tasks")
    ts = s.add_subparsers(dest="sub", required=True)
    t = ts.add_parser("create", help="package a recorded session as a task")
    t.add_argument("--session", required=True)
    t.add_argument("--instruction-file")
    t.add_argument("--id")
    t.set_defaults(fn=cmd_task_create)
    t = ts.add_parser("validate", help="run the controls and the attack suite")
    t.add_argument("task", help="task id, id@version, or a task directory")
    t.add_argument("--repeats", type=int, default=3)
    t.set_defaults(fn=cmd_task_validate)
    ts.add_parser("list", help="list tasks").set_defaults(fn=cmd_tasks)
    t = ts.add_parser("export", help="write a task to a .tar.gz")
    t.add_argument("task")
    t.add_argument("out")
    t.set_defaults(fn=cmd_task_export)
    t = ts.add_parser("delete", help="delete a task (id or id@version)")
    t.add_argument("task")
    t.set_defaults(fn=cmd_task_delete)

    s = sub.add_parser("grade", help="grade a candidate patch against a validated task")
    s.add_argument("task")
    s.add_argument("patch", help="patch file, or - for stdin")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_grade)

    a = ap.parse_args(argv)
    if a.cmd == "record" and a.command[:1] == ["--"]:
        a.command = a.command[1:]
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
