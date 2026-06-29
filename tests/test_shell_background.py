"""Tests for execute_command(background=True) — detached long-running servers.

Verifies the diagnostic-reasoning foundation: a long-running command can be
spawned detached, survives the tool call returning, writes to a log, and can
be stopped via the background_task tool. This is what lets the agent run a dev
server instead of being killed by the idle timeout.
"""

import asyncio
import os
import time

import pytest

from agent.tools.background import background_registry
from agent.tools.base import registry
from agent.tools.shell import ExecuteCommandTool


def _sleeper_script(tmp_path) -> str:
    """Write a tiny script that prints 'ready' then sleeps ~60s."""
    script = tmp_path / "sleeper.py"
    script.write_text(
        "import time, sys\n" "print('ready', flush=True)\n" "time.sleep(60)\n",
        encoding="utf-8",
    )
    return str(script)


@pytest.fixture
def shell_tool():
    return ExecuteCommandTool()


@pytest.fixture(autouse=True)
def _clean_registry():
    """Stop any leftover background tasks between tests."""
    yield
    for t in list(background_registry.list_tasks()):
        background_registry.stop(t.task_id)
    background_registry._tasks.clear()


def test_background_returns_immediately_with_task_id(shell_tool, tmp_path):
    """background=True must return at once with task_id/pid/log_path."""
    script = _sleeper_script(tmp_path)
    log = str(tmp_path / "out.log")

    async def run():
        return await shell_tool.execute(
            command=f"python3 {script}",
            cwd=str(tmp_path),
            background=True,
            log_path=log,
        )

    t0 = time.time()
    result = asyncio.run(run())
    elapsed = time.time() - t0

    assert result.success, result.error
    # Returned well before the 60s sleep — proof it didn't block.
    assert elapsed < 5
    meta = result.metadata
    assert meta["background"] is True
    assert meta["task_id"].startswith("bg_")
    assert isinstance(meta["pid"], int)
    assert meta["log_path"] == log
    assert meta["task_id"] in [t.task_id for t in background_registry.list_tasks()]


def test_background_process_survives_and_writes_log(shell_tool, tmp_path):
    """The detached process stays alive and streams output to the log."""
    script = _sleeper_script(tmp_path)
    log = str(tmp_path / "out.log")

    async def start():
        return await shell_tool.execute(
            command=f"python3 {script}",
            cwd=str(tmp_path),
            background=True,
            log_path=log,
        )

    result = asyncio.run(start())
    task_id = result.metadata["task_id"]

    # Give the process a moment to print "ready".
    time.sleep(1.0)
    assert background_registry.is_running(task_id)

    ok, body = background_registry.read_logs(task_id)
    assert ok
    assert "ready" in body


def test_background_stop_terminates_process(shell_tool, tmp_path):
    """background_task(action='stop') kills the process group."""
    script = _sleeper_script(tmp_path)
    log = str(tmp_path / "out.log")

    async def start():
        return await shell_tool.execute(
            command=f"python3 {script}",
            cwd=str(tmp_path),
            background=True,
            log_path=log,
        )

    result = asyncio.run(start())
    task_id = result.metadata["task_id"]
    pid = result.metadata["pid"]
    time.sleep(0.5)
    assert background_registry.is_running(task_id)

    ok, msg = background_registry.stop(task_id)
    assert ok
    time.sleep(0.5)
    assert not background_registry.is_running(task_id)
    # Process truly gone.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_background_does_not_add_nohup(shell_tool, tmp_path):
    """background path works without nohup — start_new_session detaches."""
    script = _sleeper_script(tmp_path)
    log = str(tmp_path / "out.log")

    async def start():
        # Plain command, no nohup / no & — the tool detaches.
        return await shell_tool.execute(
            command=f"python3 {script}",
            cwd=str(tmp_path),
            background=True,
            log_path=log,
        )

    result = asyncio.run(start())
    assert result.success
    background_registry.stop(result.metadata["task_id"])


def test_background_tool_registered():
    """The background_task tool is in the registry for the LLM to call."""
    names = [t.name for t in registry.list()]
    assert "background_task" in names
