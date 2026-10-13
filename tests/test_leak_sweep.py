import os
import shutil
import subprocess
import uuid

import pytest

from .conftest import SESSION_ENV_VAR, psutil, sweep_leaked_processes


@pytest.mark.skipif(psutil is None, reason="psutil not installed")
@pytest.mark.skipif(not shutil.which("terraform"), reason="terraform not installed")
def test_sweep_kills_leaked_terraform(tmp_path):
    terraform = shutil.which("terraform")
    assert terraform
    # a session id of its own so the sweep can't touch processes of the running tests
    session_id = uuid.uuid4().hex
    # macOS hides the environment of copied or symlinked system binaries, so leak a real terraform,
    # which waits on stdin
    leaked = subprocess.Popen(
        [terraform, "console"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        env={**os.environ, SESSION_ENV_VAR: session_id, "CHECKPOINT_DISABLE": "1"},
        start_new_session=True,
    )
    try:
        killed = sweep_leaked_processes(session_id)
        assert leaked.wait(timeout=5) is not None
    finally:
        leaked.kill()
        leaked.wait()
    assert killed == [f"{leaked.pid} {terraform} console"]
