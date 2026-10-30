"""Grade a candidate patch against a task, and validate tasks with control runs.

Every run starts from a freshly restored workspace. The candidate's edits to
tests and to anything that configures test collection are dropped, the private
test patch is applied, and the suite runs with a clean environment, plugin
autoload disabled and kiln's guard plugin loaded from outside the workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiln import gitutil, patches, sandbox, snapshot, tasks

GUARD = Path(__file__).with_name("guard_plugin.py")


@dataclass
class Outcome:
    status: str  # passed | failed | ungraded
    reward: float | None
    reason: str
    tests: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    tampered: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log: str = ""


def _uv_cache() -> str | None:
    try:
        return subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


_UV_CACHE = _uv_cache()


def _env(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
    env.update(
        HOME=str(home),
        TMPDIR=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    if _UV_CACHE:
        env["UV_CACHE_DIR"] = _UV_CACHE
    return env


def _run(
    cmd: list[str], cwd: Path, env: dict, timeout: float, confine: list[Path] | None = None
) -> tuple[int | None, str]:
    if confine is not None:
        cmd = sandbox.wrap(cmd, confine)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out.decode(errors="replace")


def _manifest(task_dir: Path) -> dict:
    return json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))


def run_suite(
    task_dir: Path,
    candidate: bytes,
    *,
    with_tests: bool = True,
    with_reference: bool = False,
) -> Outcome:
    """Raw run: per-test outcomes for start + (reference|candidate) + private tests."""
    t0 = time.monotonic()
    cfg = _manifest(task_dir)["verification"]
    with tempfile.TemporaryDirectory(prefix="kiln-run-") as tmp:
        tmp = Path(tmp)
        ws, home, guard_dir = tmp / "ws", tmp / "home", tmp / "guard"
        home.mkdir()
        guard_dir.mkdir()
        shutil.copy(GUARD, guard_dir / "kiln_guard.py")
        try:
            snapshot.restore(task_dir / "start", ws)
        except (snapshot.RestoreError, snapshot.CaptureError, gitutil.GitError) as e:
            return Outcome("ungraded", None, f"restore_failed: {e}")
        env = _env(home)

        for cmd in cfg.get("setup", []):
            code, log = _run(cmd, ws, env, cfg.get("timeout", 300))
            if code != 0:
                return Outcome("ungraded", None, "setup_failed", log=log[-4000:])

        dropped: list[str] = []
        if with_reference:
            candidate = (task_dir / "private" / "reference.patch").read_bytes()
        else:
            kept = []
            for f in patches.split(candidate):
                if any(patches.is_test_path(p) or patches.is_control_path(p) for p in f.paths):
                    dropped.append(f.path)
                else:
                    kept.append(f)
            candidate = patches.join(kept)
        if candidate.strip():
            r = gitutil.run(ws, "apply", "--binary", "--whitespace=nowarn", "-", input=candidate, check=False)
            if r.returncode != 0:
                return Outcome("failed", 0.0, "patch_does_not_apply", dropped=dropped,
                               log=r.stderr.decode(errors="replace")[-2000:])  # fmt: skip
        if with_tests:
            test_patch = (task_dir / "private" / "test.patch").read_bytes()
            r = gitutil.run(ws, "apply", "--binary", "--whitespace=nowarn", "-", input=test_patch, check=False)
            if r.returncode != 0:
                return Outcome("ungraded", None, "test_patch_does_not_apply", dropped=dropped,
                               log=r.stderr.decode(errors="replace")[-2000:])  # fmt: skip

        report = tmp / "guard.json"
        env = {**env, "PYTHONPATH": str(guard_dir), "KILN_GUARD_OUT": str(report), "KILN_WORKSPACE": str(ws)}
        cmd = list(cfg["pytest"])
        if cmd[1:3] == ["-m", "pytest"]:
            # -P: the workspace must not shadow pytest (or anything it imports).
            cmd.insert(1, "-P")
        cmd += ["-p", "kiln_guard", "-p", "no:cacheprovider", "-q", "-o", "addopts="]
        for plugin in cfg.get("plugins", []):
            cmd += ["-p", plugin]
        # Candidate code runs here: no network, writes only inside the run's temp dir.
        code, log = _run(cmd, ws, env, cfg.get("timeout", 300), confine=[tmp])
        seconds = time.monotonic() - t0
        if code is None:
            return Outcome("failed", 0.0, "timeout", dropped=dropped, seconds=seconds, log=log[-4000:])
        data = report.read_text(encoding="utf-8") if report.exists() else ""
        if not data:
            return Outcome("failed", 0.0, "no_test_report", dropped=dropped, seconds=seconds, log=log[-4000:])
        result = json.loads(data)
        tests = {}
        for nodeid, s in result["tests"].items():
            # A test passes only when the raw outcome and pytest's report agree.
            tests[nodeid] = "passed" if s["guard"] == s["report"] == "passed" else (
                s["guard"] if s["guard"] != "passed" else "mismatch"
            )
        tampered = result["tampered"] + [f"report mismatch: {n}" for n, s in tests.items() if s == "mismatch"]
        return Outcome("ran", None, "ok", tests=tests, dropped=dropped, tampered=tampered,
                       seconds=seconds, log=log[-4000:])  # fmt: skip


def grade(task_dir: Path, candidate: bytes) -> Outcome:
    """Reward 1 when every target test and every required regression passes."""
    validation_path = task_dir / "validation.json"
    if not validation_path.exists():
        return Outcome("ungraded", None, "task_not_validated")
    v = json.loads(validation_path.read_text(encoding="utf-8"))
    if v["status"] != "valid":
        return Outcome("ungraded", None, f"task_{v['status']}")
    if tasks.content_hash(task_dir) != v["content_hash"]:
        return Outcome("ungraded", None, "task_modified_after_validation")
    out = run_suite(task_dir, candidate)
    if out.status != "ran":
        return out
    if out.tampered:
        out.status, out.reward, out.reason = "failed", 0.0, "tamper_detected"
        return out
    missing = [t for t in v["targets"] + v["regressions"] if out.tests.get(t) != "passed"]
    target_fail = [t for t in v["targets"] if out.tests.get(t) != "passed"]
    if not missing:
        out.status, out.reward, out.reason = "passed", 1.0, "all_checks_passed"
    else:
        out.status, out.reward = "failed", 0.0
        out.reason = "target_failed" if target_fail else "regression_failed"
    return out


# ---------------------------------------------------------------------------
# Validation: controls and the attack suite
# ---------------------------------------------------------------------------


def _patch_from_edits(task_dir: Path, edits: dict[str, str | None]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="kiln-attack-") as tmp:
        ws = Path(tmp) / "ws"
        snapshot.restore(task_dir / "start", ws)
        gitutil.run(ws, "apply", "--binary", "-", input=(task_dir / "private" / "test.patch").read_bytes())
        gitutil.run(ws, "add", "-A", "-f")
        gitutil.run(ws, "commit", "-q", "--no-verify", "-m", "tests")
        for rel, body in edits.items():
            p = ws / rel
            if body is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")
        gitutil.run(ws, "add", "-A", "-f")
        return gitutil.run(ws, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD").stdout


_FORGE_HOOK = """\
import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    rep.outcome = "passed"
    rep.longrepr = None
"""

_SHADOW_PYTEST = """\
# Shadows the real pytest when the workspace is first on sys.path.
import json, os, sys
out = os.environ.get("KILN_GUARD_OUT")
if out:
    ids = {TESTS}
    json.dump({"tests": {t: {"guard": "passed", "report": "passed"} for t in ids},
               "tampered": [], "exitstatus": 0}, open(out, "w"))
print(f"{len(ids) if out else 5} passed in 0.01s")
sys.exit(0)
"""

ESCAPE_MARKER = Path(tempfile.gettempdir()).parent / "kiln-escape-marker"
_ESCAPE = """

# --- injected: reach outside the workspace (write a file, open a socket) ---
import socket as _ks
try:
    open({MARKER}, "w").write("escaped")
except OSError:
    pass
try:
    _ks.create_connection(("1.1.1.1", 443), timeout=3).close()
    open({MARKER} + ".net", "w").write("network")
except OSError:
    pass
"""

_IN_PROCESS = """

# --- injected: rewrite pytest's verdicts from inside the code under test ---
import _pytest.reports as _kr
_orig = _kr.TestReport.from_item_and_call
def _forged(item, call):
    rep = _orig(item, call)
    rep.outcome = "passed"
    rep.longrepr = None
    return rep
_kr.TestReport.from_item_and_call = staticmethod(_forged)
"""


def attacks(task_dir: Path, targets: list[str], regressions: list[str] = ()) -> dict[str, bytes]:
    test_files = _manifest(task_dir)["verification"]["test_files"]
    source = next(f for f in _manifest(task_dir)["reference"]["files"] if f.endswith(".py"))
    with tempfile.TemporaryDirectory(prefix="kiln-src-") as tmp:
        ws = Path(tmp) / "ws"
        snapshot.restore(task_dir / "start", ws)
        source_body = (ws / source).read_text(encoding="utf-8")
        existing_tests = [
            p.relative_to(ws).as_posix() for p in ws.rglob("*.py") if patches.is_test_path(p.relative_to(ws).as_posix())
        ]
    names = sorted({t.split("::")[-1].split("[")[0] for t in targets})
    stub = "".join(f"def {n}():\n    pass\n\n\n" for n in names)
    return {
        "forged-conftest": _patch_from_edits(task_dir, {"conftest.py": _FORGE_HOOK}),
        "rewritten-target-tests": _patch_from_edits(task_dir, {f: stub for f in test_files}),
        "deleted-tests": _patch_from_edits(task_dir, {f: None for f in test_files + existing_tests}),
        "shadowed-pytest": _patch_from_edits(
            task_dir, {"pytest.py": _SHADOW_PYTEST.replace("{TESTS}", repr(sorted(targets) + sorted(regressions)))}
        ),
        "plugin-via-config": _patch_from_edits(
            task_dir,
            {"pytest.ini": "[pytest]\naddopts = -p forge_plugin\n", "forge_plugin.py": _FORGE_HOOK},
        ),
        "in-process-report-patch": _patch_from_edits(task_dir, {source: source_body + _IN_PROCESS}),
        "escape-sandbox": _patch_from_edits(
            task_dir,
            {source: source_body + _ESCAPE.replace("{MARKER}", repr(str(ESCAPE_MARKER)))},
        ),
        "exit-before-report": _patch_from_edits(
            task_dir, {source: source_body + "\nimport os\nos._exit(0)\n"}
        ),
    }


def validate(task_dir: Path, repeats: int = 3, on_step=None) -> dict:
    step = on_step or (lambda msg: None)
    v: dict = {"status": "invalid", "checks": {}}

    def done(status: str, why: str) -> dict:
        v["status"], v["reason"] = status, why
        v["content_hash"] = tasks.content_hash(task_dir)
        (task_dir / "validation.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
        return v

    step("history: restored workspace holds only the starting commit")
    with tempfile.TemporaryDirectory(prefix="kiln-hist-") as tmp:
        ws = Path(tmp) / "ws"
        snapshot.restore(task_dir / "start", ws)
        commits = gitutil.out(ws, "rev-list", "--all").split()
        objects = gitutil.out(ws, "count-objects", "-v")
    v["checks"]["history"] = {"commits": len(commits)}
    if len(commits) != 1:
        return done("invalid", "workspace history extends beyond the start state")

    leaks = tasks.instruction_leaks((task_dir / "instruction.md").read_text(encoding="utf-8"),
                                    (task_dir / "private" / "reference.patch").read_bytes())  # fmt: skip
    v["checks"]["instruction_leak"] = leaks
    if leaks:
        return done("invalid", "instruction quotes the reference fix")

    step("control: starting state with the private tests (the problem must exist)")
    start_runs = [run_suite(task_dir, b"") for _ in range(2)]
    if any(r.status != "ran" for r in start_runs):
        bad = next(r for r in start_runs if r.status != "ran")
        v["checks"]["start"] = asdict(bad)
        return done("invalid", f"starting state could not be tested: {bad.reason}")

    step(f"control: reference fix x{repeats}")
    ref_runs = [run_suite(task_dir, b"", with_reference=True) for _ in range(repeats)]
    if any(r.status != "ran" for r in ref_runs):
        bad = next(r for r in ref_runs if r.status != "ran")
        v["checks"]["reference"] = asdict(bad)
        return done("invalid", f"reference fix could not be tested: {bad.reason}")

    all_ids = set().union(*(r.tests for r in start_runs + ref_runs))
    flaky = sorted(
        t for t in all_ids
        if len({r.tests.get(t) for r in start_runs}) > 1 or len({r.tests.get(t) for r in ref_runs}) > 1
    )  # fmt: skip
    before, after = start_runs[0].tests, ref_runs[0].tests
    test_files = set(_manifest(task_dir)["verification"]["test_files"])
    targets = sorted(
        t for t in all_ids
        if t not in flaky and before.get(t) != "passed" and after.get(t) == "passed"
    )  # fmt: skip
    regressions = sorted(t for t in all_ids if t not in flaky and before.get(t) == after.get(t) == "passed")
    preexisting = sorted(t for t in all_ids if t not in flaky and before.get(t) != "passed" and after.get(t) != "passed")
    broken_by_ref = sorted(t for t in all_ids if before.get(t) == "passed" and after.get(t) not in ("passed", None))
    v.update(targets=targets, regressions=regressions, preexisting_failures=preexisting, flaky=flaky)
    v["checks"]["controls"] = {
        "start_seconds": [round(r.seconds, 2) for r in start_runs],
        "reference_seconds": [round(r.seconds, 2) for r in ref_runs],
        "targets_outside_test_patch": [t for t in targets if t.split("::")[0] not in test_files],
        "broken_by_reference": broken_by_ref,
    }
    if broken_by_ref:
        return done("invalid", "the reference fix breaks tests that passed before")
    if flaky and not targets:
        return done("quarantined", "every candidate target test is flaky")
    if not targets:
        return done("invalid", "no test fails before the fix and passes after it")

    # Freeze the sets so grade() can be used for the remaining controls.
    v["status"] = "valid"
    done("valid", "provisional")

    step("control: reference fix through the grader")
    ref = grade(task_dir, (task_dir / "private" / "reference.patch").read_bytes())
    step("control: empty patch")
    null = grade(task_dir, b"")
    v["checks"]["graded_reference"] = {"reward": ref.reward, "reason": ref.reason}
    v["checks"]["graded_empty"] = {"reward": null.reward, "reason": null.reason}

    results = {}
    escape_files = [ESCAPE_MARKER, ESCAPE_MARKER.with_name(ESCAPE_MARKER.name + ".net")]
    for f in escape_files:
        f.unlink(missing_ok=True)
    for name, patch in attacks(task_dir, targets, regressions).items():
        step(f"attack: {name}")
        o = grade(task_dir, patch)
        results[name] = {"reward": o.reward, "status": o.status, "reason": o.reason,
                         "dropped": o.dropped, "tampered": o.tampered}  # fmt: skip
    escaped = [f.name for f in escape_files if f.exists()]
    for f in escape_files:
        f.unlink(missing_ok=True)
    v["checks"]["attacks"] = results
    v["checks"]["containment"] = {"sandbox": sandbox.available(), "escaped": escaped}
    if escaped:
        return done("invalid", "candidate code reached outside its workspace: " + ", ".join(escaped))

    if ref.reward != 1.0:
        return done("invalid", f"reference fix scored {ref.reward} ({ref.reason})")
    if null.reward != 0.0:
        return done("invalid", f"empty patch scored {null.reward} ({null.reason})")
    beaten = [n for n, r in results.items() if r["reward"] != 0.0]
    if beaten:
        return done("invalid", "verifier accepted attack patches: " + ", ".join(beaten))
    return done("valid", "all controls and attacks behaved as expected")


def describe(o: Outcome) -> str:
    parts = [f"{o.status}", f"reward={o.reward}", o.reason]
    if o.dropped:
        parts.append("dropped=" + ",".join(o.dropped))
    if o.tampered:
        parts.append("tampered=" + ";".join(o.tampered))
    return "  ".join(parts)

