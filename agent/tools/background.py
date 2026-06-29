"""Background process management — registry + tool.

Long-running servers (npm run dev, next dev, npx serve, uvicorn, ...) cannot
run inside the blocking `execute_command` loop: `read_process` kills any
process that goes silent for the idle timeout, and foregrounding them hangs
the agent. `execute_command(background=True)` spawns such commands detached
(via `start_new_session=True`, which replaces the need for `nohup`), redirects
their output to a log file, and registers them here so the agent can:

  * list running background tasks,
  * read their logs (the diagnostic loop on start failure),
  * check status, and
  * stop them by signalling the process group.

The registry is module-level (process-wide) so background tasks outlive a
single tool call and can be managed across turns.
"""

import os
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .base import BaseTool, ToolResult, registry


@dataclass
class BackgroundTask:
    """A detached long-running process tracked by the registry."""

    task_id: str
    pid: int
    proc: object  # asyncio.subprocess.Process
    log_path: str
    command: str
    cwd: Optional[str]
    started_at: float


class BackgroundTaskRegistry:
    """Process-wide registry of background tasks."""

    def __init__(self):
        self._tasks: dict[str, BackgroundTask] = {}

    def register(
        self,
        proc: object,
        log_path: str,
        command: str,
        cwd: Optional[str] = None,
    ) -> str:
        """Register a freshly-spawned background process. Returns task_id."""
        task_id = f"bg_{uuid.uuid4().hex[:8]}"
        self._tasks[task_id] = BackgroundTask(
            task_id=task_id,
            pid=proc.pid,
            proc=proc,
            log_path=log_path,
            command=command,
            cwd=cwd,
            started_at=time.time(),
        )
        return task_id

    def get(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def is_running(self, task_id: str) -> bool:
        """True if the process is still alive."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        proc = task.proc
        # asyncio Process sets returncode once reaped; None means still running.
        if getattr(proc, "returncode", None) is not None:
            return False
        # Probe with kill(pid, 0) — returns if alive, raises if not.
        try:
            os.kill(task.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
        return True

    def stop(self, task_id: str) -> tuple[bool, str]:
        """Signal the process group to terminate. Returns (ok, message)."""
        task = self._tasks.get(task_id)
        if not task:
            return False, f"Unknown task_id: {task_id}"
        if not self.is_running(task_id):
            return True, f"Task {task_id} already stopped"
        # start_new_session=True made the child a process-group leader, so
        # killpg(pgid) reaches the server and any children it spawned.
        try:
            pgid = os.getpgid(task.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True, f"Task {task_id} already stopped"
        except Exception as exc:
            # Fallback: kill the leader directly.
            try:
                task.proc.kill()
            except Exception:
                pass
            return True, f"Task {task_id} stopped (killpg failed: {exc}; used proc.kill)"
        # Give the registry's view a chance to update.
        try:
            task.proc.returncode = -15  # -15 = killed by SIGTERM
        except Exception:
            pass
        return True, f"Task {task_id} stopped (SIGTERM to pgid {pgid})"

    def read_logs(self, task_id: str, lines: int = 50) -> tuple[bool, str]:
        """Tail the log file. Returns (ok, content)."""
        task = self._tasks.get(task_id)
        if not task:
            return False, f"Unknown task_id: {task_id}"
        try:
            with open(task.log_path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
        except FileNotFoundError:
            return True, f"(log {task.log_path} not yet created — no output yet)"
        except Exception as exc:
            return False, f"Failed to read log {task.log_path}: {exc}"
        tail = all_lines[-lines:] if lines > 0 else all_lines
        body = "".join(tail).rstrip()
        return True, body or "(empty log — process may still be starting)"


# Global registry instance.
background_registry = BackgroundTaskRegistry()


class BackgroundTaskTool(BaseTool):
    """Manage background processes spawned via `execute_command(background=True)`."""

    name = "background_task"
    description = (
        "Manage background processes (dev servers, watchers) started with "
        "execute_command(background=true). Actions: list running tasks, read "
        "a task's log (use this to diagnose why a server failed to start), "
        "check status, or stop a task."
    )
    user_facing_name = "Bg"
    is_destructive = True  # 'stop' kills processes
    is_read_only = False

    def render_call(self, args: dict) -> str:
        action = args.get("action", "?")
        tid = args.get("task_id", "")
        return f"background_task {action} {tid}".strip()

    async def execute(
        self,
        action: str = "list",
        task_id: Optional[str] = None,
        lines: int = 50,
        **kwargs,
    ) -> ToolResult:
        action = (action or "list").lower()

        if action == "list":
            tasks = background_registry.list_tasks()
            if not tasks:
                return ToolResult(success=True, content="No background tasks.")
            rows = []
            for t in tasks:
                running = background_registry.is_running(t.task_id)
                age = int(time.time() - t.started_at)
                status = "running" if running else "stopped"
                rows.append(
                    f"- {t.task_id} [{status}] pid={t.pid} age={age}s "
                    f"cmd=`{t.command[:60]}` log={t.log_path}"
                )
            return ToolResult(success=True, content="\n".join(rows))

        if action == "logs":
            if not task_id:
                return ToolResult(success=False, content="", error="logs requires task_id")
            ok, body = background_registry.read_logs(task_id, lines=lines)
            return ToolResult(success=ok, content=body, error=None if ok else body)

        if action == "status":
            if not task_id:
                return ToolResult(success=False, content="", error="status requires task_id")
            t = background_registry.get(task_id)
            if not t:
                return ToolResult(success=False, content="", error=f"Unknown task_id: {task_id}")
            running = background_registry.is_running(task_id)
            rc = getattr(t.proc, "returncode", None)
            return ToolResult(
                success=True,
                content=(
                    f"task_id={t.task_id} running={running} pid={t.pid} "
                    f"returncode={rc} cmd=`{t.command[:60]}` log={t.log_path}"
                ),
            )

        if action == "stop":
            if not task_id:
                return ToolResult(success=False, content="", error="stop requires task_id")
            ok, msg = background_registry.stop(task_id)
            return ToolResult(success=ok, content=msg, error=None if ok else msg)

        return ToolResult(
            success=False,
            content="",
            error=f"Unknown action '{action}'. Use: list, logs, status, stop.",
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "logs", "status", "stop"],
                            "description": "list=show all background tasks; "
                            "logs=tail a task's log (diagnose start failures); "
                            "status=check if running; stop=terminate the process group.",
                            "default": "list",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Required for logs/status/stop; ignored by list.",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Number of trailing log lines for 'logs'.",
                            "default": 50,
                        },
                    },
                    "required": ["action"],
                },
            },
        }


registry.register(BackgroundTaskTool())
