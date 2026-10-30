"""Project layout, configuration and the local metadata database.

Everything kiln keeps for a repository lives under `<repo>/.kiln/`, which is
added to `.git/info/exclude` so it never shows up in the developer's diffs.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from kiln import gitutil

DIR = ".kiln"

DEFAULT_CONFIG = """\
# kiln project configuration.
# Commands are argument lists, run from the root of a fresh task workspace.

[verify]
# Prepare a fresh workspace (create the environment, install the project).
setup = [
  ["uv", "venv", "-q", ".venv"],
  ["uv", "pip", "install", "-q", "--python", ".venv/bin/python", "pytest", "-e", "."],
]
# Run tests. kiln appends its own report and selection arguments.
pytest = [".venv/bin/python", "-m", "pytest"]
# Seconds before a test run is stopped and the attempt counts as failed.
timeout = 300
# Entry-point pytest plugins are not loaded unless listed here.
plugins = []

[capture]
# Untracked files larger than this are not snapshotted (the capture fails instead).
max_untracked_mb = 50
"""

_SCHEMA = [
    """
    CREATE TABLE snapshots (
        id TEXT PRIMARY KEY,
        created TEXT NOT NULL,
        head TEXT NOT NULL,
        branch TEXT,
        state_hash TEXT NOT NULL,
        path TEXT NOT NULL
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        agent TEXT NOT NULL,
        source TEXT,
        started TEXT,
        ended TEXT,
        status TEXT NOT NULL,
        exit_code INTEGER,
        start_snapshot TEXT REFERENCES snapshots(id),
        end_snapshot TEXT REFERENCES snapshots(id),
        final_patch TEXT,
        events INTEGER DEFAULT 0,
        completeness TEXT
    );
    CREATE TABLE tasks (
        id TEXT NOT NULL,
        version INTEGER NOT NULL,
        session_id TEXT REFERENCES sessions(id),
        status TEXT NOT NULL,
        created TEXT NOT NULL,
        path TEXT NOT NULL,
        content_hash TEXT,
        PRIMARY KEY (id, version)
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        task_version INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        reward REAL,
        reason TEXT,
        created TEXT NOT NULL,
        details TEXT
    );
    """,
]


@dataclass
class Project:
    root: Path

    @property
    def dir(self) -> Path:
        return self.root / DIR

    @property
    def config_path(self) -> Path:
        return self.dir / "config.toml"

    def config(self) -> dict:
        return tomllib.loads(self.config_path.read_text(encoding="utf-8"))

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.dir / "kiln.db")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(_SCHEMA[version:], start=version + 1):
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()
        return conn


def init(start: Path) -> Project:
    root = gitutil.toplevel(start)
    project = Project(root)
    project.dir.mkdir(exist_ok=True)
    if not project.config_path.exists():
        project.config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    exclude = Path(gitutil.git_dir(root)) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if f"/{DIR}/" not in lines:
        exclude.write_text("\n".join([*lines, f"/{DIR}/"]) + "\n", encoding="utf-8")
    project.db().close()
    return project


def find(start: Path) -> Project:
    root = gitutil.toplevel(start)
    project = Project(root)
    if not project.config_path.exists():
        raise SystemExit(f"{root} is not a kiln project. Run `kiln init` first.")
    return project


def dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)
