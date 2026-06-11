"""Tools - Base class and registry"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


class IdleTimeoutError(asyncio.TimeoutError):
    """Raised when a process is killed due to idle timeout.
    Carries partial output so callers can show what happened before the hang.
    """

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


async def read_process(proc: asyncio.subprocess.Process, idle_timeout: int = 60) -> tuple:
    """Read stdout/stderr until process exits, killing if idle for idle_timeout seconds.

    Idle = no data on EITHER stdout or stderr for idle_timeout seconds.
    Data on any one stream resets the timer. The total run time is unlimited
    as long as the process keeps producing output.

    Returns (stdout_str, stderr_str).
    """
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stdout_done = False
    stderr_done = False

    async def _read_chunk(stream) -> bytes | None:
        return await stream.read(4096)

    while not (stdout_done and stderr_done):
        # Process exited — drain remaining streams without timeout
        if proc.returncode is not None:
            if not stdout_done:
                chunk = await proc.stdout.read()
                if chunk:
                    stdout_parts.append(chunk)
                stdout_done = True
            if not stderr_done:
                chunk = await proc.stderr.read()
                if chunk:
                    stderr_parts.append(chunk)
                stderr_done = True
            break

        # Both streams active — apply idle timeout
        tasks = {}
        if not stdout_done:
            tasks[asyncio.ensure_future(_read_chunk(proc.stdout))] = "stdout"
        if not stderr_done:
            tasks[asyncio.ensure_future(_read_chunk(proc.stderr))] = "stderr"

        if not tasks:
            break

        done, pending = await asyncio.wait(
            tasks.keys(), timeout=idle_timeout, return_when=asyncio.FIRST_COMPLETED
        )

        if not done:
            proc.kill()
            await proc.wait()
            partial_stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
            partial_stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
            raise IdleTimeoutError(
                f"No output for {idle_timeout}s (process killed)",
                stdout=partial_stdout,
                stderr=partial_stderr,
            )

        for task in pending:
            task.cancel()

        for task in done:
            name = tasks[task]
            try:
                chunk = task.result()
            except Exception:
                chunk = None
            if chunk:
                if name == "stdout":
                    stdout_parts.append(chunk)
                else:
                    stderr_parts.append(chunk)
            else:
                if name == "stdout":
                    stdout_done = True
                else:
                    stderr_done = True

    return (
        b"".join(stdout_parts).decode("utf-8", errors="replace"),
        b"".join(stderr_parts).decode("utf-8", errors="replace"),
    )


@dataclass
class ToolResult:
    success: bool
    content: str
    error: Optional[str] = None
    metadata: Optional[dict] = None  # e.g. {"lines": 42, "duration_ms": 350, "matches": 5}


class BaseTool(ABC):
    """Base class for all tools — fail-closed defaults for safety."""

    name: str = ""
    description: str = ""

    # ── Metadata ────────────────────────────────────────────────
    is_concurrency_safe: bool = False  # Can run in parallel with other reads
    is_read_only: bool = False  # Does not modify files or system state
    is_destructive: bool = False  # Can cause irreversible damage (rm, format)

    # ── UX ──────────────────────────────────────────────────────
    user_facing_name: str = ""  # Short badge name, e.g. "Read", "Bash", "Write"
    interrupt_behavior: str = "cancel"  # "cancel" or "block" — how to handle Ctrl+C

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool."""
        pass

    # ── Gating ──────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """Whether this tool is currently available. Override for feature flags."""
        return True

    # ── Permission check (per-tool override) ─────────────────────

    def check_permissions(self, args: dict) -> Tuple[bool, str]:
        """Per-tool permission validation. Override to add tool-specific checks.

        Returns (allowed, reason). Default: always allow.
        """
        return True, ""

    # ── UI rendering (per-tool override) ─────────────────────────

    def get_activity_description(self, args: dict) -> str:
        """Short verb phrase for spinner display, e.g. 'Reading file...'"""
        if self.user_facing_name:
            return self.user_facing_name + "..."
        return self.name + "..."

    def render_call(self, args: dict) -> str:
        """Render a tool call for CLI display. Override for tool-specific formatting."""
        key = next(iter(args)) if args else ""
        val = str(args.get(key, ""))[:60]
        return f"{self.name}: {key}={val}" if key else self.name

    def render_result(self, result: ToolResult) -> str:
        """Render a tool result for CLI display. Override for tool-specific formatting."""
        if result.success and result.content:
            return result.content.split("\n")[0][:80]
        if not result.success and result.error:
            return result.error[:120]
        return ""

    # ── Prompt contribution (per-tool override) ──────────────────

    def prompt_contribution(self) -> str:
        """Optional section added to the system prompt. Override to provide
        usage hints, example patterns, or constraints specific to this tool."""
        return ""

    @property
    def schema(self) -> dict:
        """Return JSON schema for tool definition"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}},
        }


# ── build_tool factory ──────────────────────────────────────────


def build_tool(
    name: str,
    description: str,
    execute_fn: Callable,
    *,
    is_concurrency_safe: bool = False,
    is_read_only: bool = False,
    is_destructive: bool = False,
    user_facing_name: str = "",
    interrupt_behavior: str = "cancel",
    is_enabled: Optional[Callable[[], bool]] = None,
    schema_override: Optional[dict] = None,
    check_permissions: Optional[Callable[[dict], Tuple[bool, str]]] = None,
    get_activity_description: Optional[Callable[[dict], str]] = None,
    render_call: Optional[Callable[[dict], str]] = None,
    render_result: Optional[Callable[[ToolResult], str]] = None,
    prompt_contribution: Optional[Callable[[], str]] = None,
):
    """Create a tool with safe defaults (fail-closed).

    Only override what you need — everything else gets a safe default.
    This is the recommended way to create new tools.

    Example:
        def my_tool_execute(**kwargs) -> ToolResult:
            return ToolResult(success=True, content="done")

        tool = build_tool(
            name="my_tool",
            description="Does something useful",
            execute_fn=my_tool_execute,
            is_read_only=True,
        )
        registry.register(tool)
    """

    # Build a concrete tool class with execute bound at the class level
    async def _execute(self, **kwargs):
        return execute_fn(**kwargs)

    import re as _re

    safe_name = _re.sub(r"[^a-zA-Z0-9_]", "_", name)
    _BuiltTool = type(f"_Built_{safe_name}", (BaseTool,), {"execute": _execute})

    tool = _BuiltTool()
    tool.name = name
    tool.description = description
    tool.is_concurrency_safe = is_concurrency_safe
    tool.is_read_only = is_read_only
    tool.is_destructive = is_destructive
    tool.user_facing_name = user_facing_name or name
    tool.interrupt_behavior = interrupt_behavior

    if is_enabled:
        tool.is_enabled = is_enabled
    if check_permissions:
        tool.check_permissions = check_permissions
    if get_activity_description:
        tool.get_activity_description = get_activity_description
    if render_call:
        tool.render_call = render_call
    if render_result:
        tool.render_result = render_result
    if prompt_contribution:
        tool.prompt_contribution = prompt_contribution

    # Schema
    if schema_override:
        tool.schema = property(lambda self: schema_override)

    return tool


class ToolRegistry:
    """Registry of available tools"""

    def __init__(self):
        self._tools = {}

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list(self) -> list:
        return list(self._tools.values())

    @property
    def schemas(self) -> list:
        # Wrap each tool schema in OpenAI's expected format:
        # {"type": "function", "function": {...}}
        # Some tools already return the wrapped format; avoid double-wrapping.
        result = []
        for t in self._tools.values():
            s = t.schema
            if isinstance(s, dict) and s.get("type") == "function" and "function" in s:
                result.append(s)
            else:
                result.append({"type": "function", "function": s})
        return result


# Global registry
registry = ToolRegistry()
