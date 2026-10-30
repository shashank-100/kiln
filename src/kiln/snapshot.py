"""Exact, portable snapshots of a working tree.

A snapshot holds the committed source as a plain archive (no history), the
staged and unstaged changes as separate binary patches, and the untracked files
as a tarball. Restoring builds a fresh repository whose only commit is the base,
so nothing after the snapshot (or before it) is reachable from inside a task.
Every file is hashed at capture time and checked again after restore.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
from pathlib import Path

from kiln import gitutil
from kiln.project import Project, dump

SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    ".netrc", ".npmrc", ".pypirc", "credentials*.json", "*.keystore",
)  # fmt: skip
LOCKFILES = (
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.lock", "go.sum",
)  # fmt: skip
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class CaptureError(RuntimeError):
    """The working tree can't be captured faithfully; nothing was written."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(path: Path) -> dict | None:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return {"link": os.readlink(path)}
    if not stat.S_ISREG(st.st_mode):
        return None
    return {"sha256": sha256(path.read_bytes()), "exec": bool(st.st_mode & stat.S_IXUSR)}


def _is_secret(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def _z(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def state_of(repo: Path, files: list[str]) -> dict[str, dict]:
    manifest = {}
    for rel in sorted(files):
        p = repo / rel
        if p.is_symlink() or p.exists():
            if (e := _entry(p)) is not None:
                manifest[rel] = e
    return manifest


def walk_state(root: Path, skip: tuple[str, ...] = (".git",)) -> dict[str, dict]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, f), root))
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                files.append(os.path.relpath(os.path.join(dirpath, d), root))
                dirnames.remove(d)
    return state_of(root, files)


def state_hash(manifest: dict) -> str:
    return sha256(json.dumps(manifest, sort_keys=True).encode())


def capture(project: Project, label: str) -> str:
    repo = project.root
    cfg = project.config().get("capture", {})
    max_bytes = int(cfg.get("max_untracked_mb", 50)) * 1024 * 1024

    head = gitutil.run(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if head.returncode != 0:
        raise CaptureError("the repository has no commits yet; commit once before capturing")
    head_sha = head.stdout.decode().strip()
    branch = gitutil.run(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if (repo / ".gitmodules").exists():
        raise CaptureError("submodules are not supported yet")

    tracked = _z(gitutil.run(repo, "ls-files", "-z", "--cached").stdout)
    untracked_all = _z(gitutil.run(repo, "ls-files", "-z", "--others", "--exclude-standard").stdout)
    untracked = [p for p in untracked_all if not _is_secret(p) and not p.startswith(".kiln/")]
    excluded = sorted(p for p in untracked_all if _is_secret(p))

    for rel in tracked:
        p = repo / rel
        if p.is_file() and not p.is_symlink() and p.stat().st_size < 1024:
            if p.read_bytes().startswith(LFS_POINTER):
                raise CaptureError(f"Git LFS object not available locally: {rel}")
    attrs = repo / ".gitattributes"
    if attrs.exists() and "filter=lfs" in attrs.read_text(errors="replace"):
        raise CaptureError("Git LFS-tracked files are not supported yet")
    too_big = [p for p in untracked if (repo / p).is_file() and (repo / p).stat().st_size > max_bytes]
    if too_big:
        raise CaptureError(f"untracked files over the size limit: {', '.join(too_big[:5])}")

    manifest = state_of(repo, tracked + untracked)
    s_hash = state_hash(manifest)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
    snap_id = f"snap-{stamp}-{s_hash[:8]}"
    out = project.path("snapshots", snap_id, "x").parent

    base = gitutil.run(repo, "archive", "--format=tar", head_sha).stdout
    staged = gitutil.run(repo, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", head_sha).stdout
    unstaged = gitutil.run(repo, "diff", "--binary", "--no-color", "--no-ext-diff").stdout
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in sorted(untracked):
            tar.add(repo / rel, arcname=rel, recursive=False)
    artifacts = {
        "base.tar": base,
        "staged.patch": staged,
        "unstaged.patch": unstaged,
        "untracked.tar": buf.getvalue(),
        "manifest.json": dump(manifest).encode(),
    }
    for name, data in artifacts.items():
        (out / name).write_bytes(data)

    lockfiles = {
        rel: manifest[rel]["sha256"]
        for rel in manifest
        if "sha256" in manifest[rel] and any(fnmatch.fnmatch(rel.rsplit("/", 1)[-1], g) for g in LOCKFILES)
    }
    meta = {
        "id": snap_id,
        "label": label,
        "created": dt.datetime.now(dt.UTC).isoformat(),
        "repo": str(repo),
        "head": head_sha,
        "branch": branch.stdout.decode().strip() or None,
        "state_hash": s_hash,
        "files": len(manifest),
        "untracked": len(untracked),
        "excluded_secrets": excluded,
        "lockfiles": lockfiles,
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "artifacts": {name: sha256(data) for name, data in artifacts.items()},
    }
    (out / "meta.json").write_text(dump(meta), encoding="utf-8")
    with project.db() as db:
        db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (snap_id, meta["created"], head_sha, meta["branch"], s_hash, str(out)),
        )
    return snap_id


def load_meta(snap_dir: Path) -> dict:
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    for name, digest in meta["artifacts"].items():
        if sha256((snap_dir / name).read_bytes()) != digest:
            raise CaptureError(f"snapshot artifact {name} does not match its recorded hash")
    return meta


class RestoreError(RuntimeError):
    pass


def restore(snap_dir: Path, dest: Path, *, commit: bool = True) -> dict:
    """Rebuild a snapshot in `dest` and prove it matches. Returns the snapshot metadata.

    With `commit`, the restored state becomes the single commit `start`, so a
    later `git diff start` is exactly what changed in the workspace.
    """
    meta = load_meta(snap_dir)
    if dest.exists() and any(dest.iterdir()):
        raise RestoreError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(snap_dir / "base.tar") as tar:
        tar.extractall(dest, filter="tar")
    gitutil.run(dest, "init", "-q", "-b", "kiln")
    gitutil.run(dest, "add", "-A", "-f")
    gitutil.run(dest, "commit", "-q", "--allow-empty", "--no-verify", "-m", "base")
    for name, extra in (("staged.patch", ["--index"]), ("unstaged.patch", [])):
        patch = (snap_dir / name).read_bytes()
        if patch.strip():
            gitutil.run(dest, "apply", "--binary", "--whitespace=nowarn", *extra, "-", input=patch)
    with tarfile.open(snap_dir / "untracked.tar") as tar:
        tar.extractall(dest, filter="tar")

    expected = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    actual = walk_state(dest)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
        raise RestoreError(
            f"restored state differs: missing={missing[:5]} extra={extra[:5]} changed={changed[:5]}"
        )
    if commit:
        # Squash to one commit so the base and the overlays are indistinguishable history-free.
        shutil.rmtree(dest / ".git")
        gitutil.run(dest, "init", "-q", "-b", "kiln")
        gitutil.run(dest, "add", "-A", "-f")
        gitutil.run(dest, "commit", "-q", "--allow-empty", "--no-verify", "-m", "start")
        gitutil.run(dest, "tag", "start")
    return meta


def diff_states(start_dir: Path, end_dir: Path, scratch: Path) -> bytes:
    """Binary patch turning restored snapshot `start_dir` into `end_dir`'s files."""
    work = scratch / "diff"
    restore(start_dir, work)
    for child in work.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    end_work = scratch / "end"
    restore(end_dir, end_work, commit=False)
    for child in end_work.iterdir():
        if child.name == ".git":
            continue
        target = work / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)
    gitutil.run(work, "add", "-A", "-f")
    return gitutil.run(work, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "start").stdout
