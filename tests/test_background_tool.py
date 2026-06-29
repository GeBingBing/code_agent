"""Tests for the background_task management tool (list / logs / status / stop)."""

import asyncio
import time

import pytest

from agent.tools.background import BackgroundTaskTool, background_registry
from agent.tools.shell import ExecuteCommandTool


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for t in list(background_registry.list_tasks()):
        background_registry.stop(t.task_id)
    background_registry._tasks.clear()


def _start_sleeper(tmp_path, tool=None):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    shell = tool or ExecuteCommandTool()
    log = str(tmp_path / "out.log")

    async def start():
        return await shell.execute(
            command=f"python3 {script}",
            cwd=str(tmp_path),
            background=True,
            log_path=log,
        )

    return asyncio.run(start()).metadata["task_id"]


def test_list_empty():
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="list"))
    assert result.success
    assert "No background tasks" in result.content


def test_list_shows_running_task(tmp_path):
    task_id = _start_sleeper(tmp_path)
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="list"))
    assert result.success
    assert task_id in result.content
    assert "running" in result.content


def test_logs_returns_tail(tmp_path):
    task_id = _start_sleeper(tmp_path)
    time.sleep(1.0)  # let it print "ready"
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="logs", task_id=task_id))
    assert result.success
    assert "ready" in result.content


def test_logs_unknown_task():
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="logs", task_id="bg_nope"))
    assert not result.success


def test_status_running(tmp_path):
    task_id = _start_sleeper(tmp_path)
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="status", task_id=task_id))
    assert result.success
    assert "running=True" in result.content


def test_stop_terminates(tmp_path):
    task_id = _start_sleeper(tmp_path)
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="stop", task_id=task_id))
    assert result.success
    time.sleep(0.3)
    assert not background_registry.is_running(task_id)


def test_stop_already_stopped(tmp_path):
    task_id = _start_sleeper(tmp_path)
    tool = BackgroundTaskTool()
    asyncio.run(tool.execute(action="stop", task_id=task_id))
    result = asyncio.run(tool.execute(action="stop", task_id=task_id))
    assert result.success
    assert "already stopped" in result.content


def test_unknown_action():
    tool = BackgroundTaskTool()
    result = asyncio.run(tool.execute(action="frobnicate"))
    assert not result.success
    assert "Unknown action" in (result.error or "")
