"""pytest plugin kiln loads from outside the workspace for every graded run.

It records each test's outcome from the raw call information rather than from
the report object, and checks at the end that nothing replaced pytest's
reporting machinery or registered reporting hooks from candidate source.
Results go to a file descriptor opened before any project code is imported.
"""

import json
import os
import sys
import sysconfig

import _pytest.reports as _reports
import _pytest.runner as _runner
import pytest

_fd = os.open(os.environ.pop("KILN_GUARD_OUT"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
_workspace = os.path.realpath(os.environ.pop("KILN_WORKSPACE"))
# The interpreter's own install (created by setup from the trusted start state)
# may live inside the workspace; code installed there is not candidate source.
_trusted = tuple(
    os.path.realpath(p) + os.sep
    for p in {sys.prefix, sys.base_prefix, sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
)


def _watched():
    return {
        "TestReport.from_item_and_call": _reports.TestReport.__dict__["from_item_and_call"].__func__,
        "CallInfo.from_call": _runner.CallInfo.__dict__["from_call"].__func__,
        "runner.call_and_report": _runner.call_and_report,
        "runner.call_runtest_hook": getattr(_runner, "call_runtest_hook", None),
        "runner.runtestprotocol": _runner.runtestprotocol,
    }


_ORIGINAL = _watched()
_SELF = None
_phases = {}
_reported = {}
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
_SKIPS = (pytest.skip.Exception, pytest.xfail.Exception)


def pytest_configure(config):
    global _SELF
    _SELF = pytest_runtest_makereport


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.excinfo is None:
        status = "passed"
    elif call.excinfo.errisinstance(_SKIPS):
        status = "skipped"
    else:
        status = "failed" if call.when == "call" else "error"
    _phases.setdefault(item.nodeid, {})[call.when] = status
    try:
        _reported.setdefault(item.nodeid, {})[call.when] = outcome.get_result().outcome
    except BaseException:
        _reported.setdefault(item.nodeid, {})[call.when] = "error"


def _final(phases):
    worst = max(phases.values(), key=_RANK.__getitem__)
    if worst in ("failed", "error"):
        return worst
    if phases.get("call") == "passed":
        return "passed"
    return "skipped"


def pytest_sessionfinish(session, exitstatus):
    tampered = [name for name, fn in _watched().items() if fn is not _ORIGINAL[name]]
    if pytest_runtest_makereport is not _SELF:
        tampered.append("kiln guard hook")
    pm = session.config.pluginmanager
    for hook_name in ("pytest_runtest_makereport", "pytest_runtest_logreport", "pytest_report_teststatus"):
        for impl in getattr(pm.hook, hook_name).get_hookimpls():
            path = getattr(impl.plugin, "__file__", "") or ""
            real = os.path.realpath(path) if path else ""
            if (
                real.startswith(_workspace + os.sep)
                and not real.startswith(_trusted)
                and os.path.basename(real) != "conftest.py"
            ):
                tampered.append(f"{hook_name} from {os.path.relpath(real, _workspace)}")
    tests = {}
    for nodeid, phases in _phases.items():
        reported = _reported.get(nodeid, {})
        tests[nodeid] = {"guard": _final(phases), "report": reported.get("call", reported.get("setup", ""))}
    os.write(_fd, json.dumps({"tests": tests, "tampered": tampered, "exitstatus": int(exitstatus)}).encode())
    os.close(_fd)
