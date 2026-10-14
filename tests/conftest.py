"""Kill processes the tests leaked, e.g. tofu and its provider plugins, when the session ends."""

import os
import sys
import uuid
from typing import List

import pytest

try:
    import psutil
except ImportError:
    psutil = None

SESSION_ENV_VAR = "UNFURL_TEST_SESSION_ID"
LEAKABLE_PREFIXES = ("terraform", "tofu", "kompose")  # includes terraform-provider-*


def pytest_configure(config: pytest.Config) -> None:
    # inherited by xdist workers and everything the tests spawn
    os.environ.setdefault(SESSION_ENV_VAR, uuid.uuid4().hex)


def sweep_leaked_processes(session_id: str, grace_period: float = 3) -> List[str]:
    """Kill the leakable processes started with ``session_id`` and return their descriptions."""
    assert psutil
    leaked = []
    for proc in psutil.process_iter(["name"]):
        if not (proc.info["name"] or "").startswith(LEAKABLE_PREFIXES):
            continue
        try:
            if proc.environ().get(SESSION_ENV_VAR) == session_id:
                leaked.append((proc, f"{proc.pid} {' '.join(proc.cmdline())}"))
        except psutil.Error:  # exited, or not ours to inspect
            pass
    procs = [proc for proc, _ in leaked]
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace_period)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    return [description for _, description in leaked]


def _report(session: pytest.Session, message: str) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.ensure_newline()
        reporter.write_line(message, yellow=True)
    else:
        print(message, file=sys.stderr)


def pytest_sessionfinish(session: pytest.Session) -> None:
    # xdist workers skip this so the controller sweeps once, even after a worker crashed
    if hasattr(session.config, "workerinput"):
        return
    if psutil is None:
        _report(session, "WARNING: psutil isn't installed, skipped the leaked process sweep")
        return
    killed = sweep_leaked_processes(os.environ[SESSION_ENV_VAR])
    if killed:
        _report(
            session,
            "WARNING: killed processes leaked by the tests:\n  " + "\n  ".join(killed),
        )
