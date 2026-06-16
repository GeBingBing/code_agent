"""Shell execution tool"""

import asyncio
import re
import shlex
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .base import BaseTool, IdleTimeoutError, ToolResult, read_process, registry

# Whitelist of safe commands
SAFE_COMMANDS = {
    "ls",
    "cat",
    "grep",
    "find",
    "echo",
    "python",
    "python3",
    "node",
    "npm",
    "pip",
    "pip3",
    "git",
    "cd",
    "pwd",
    "mkdir",
    "rm",
    "cp",
    "mv",
    "touch",
    "chmod",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "awk",
    "sed",
    "cut",
    "xargs",
    "which",
    "file",
    "stat",
    "diff",
    "patch",
    "make",
    "cmake",
    "cargo",
    "rustc",
    "go",
    "java",
    "javac",
    "ruby",
    "perl",
    "bash",
    "sh",
    "zsh",
    "fish",
    "curl",
    "wget",
    "tar",
    "gzip",
    "gunzip",
    "zip",
    "unzip",
    "rsync",
    "scp",
    "ssh",
    "ping",
    "nc",
    "netstat",
    "ps",
    "top",
    "htop",
    "kill",
    "killall",
    "pkill",
    "pgrep",
    "free",
    "df",
    "du",
    "lsblk",
    "mount",
    "umount",
    "fdisk",
    "mkfs.ext4",
    # Package managers
    "brew",
    "apt",
    "apt-get",
    "dnf",
    "yum",
    "pacman",
    "conda",
    "gem",
    "snap",
    "npx",
    "pnpm",
    "yarn",
    "poetry",
    "mamba",
    "pipx",
}

# Blocked command patterns (more robust than simple string matching)
BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/\s*",
    r"rm\s+-rf\s+~",
    r"rm\s+-rf\s+/\*",
    r"sudo\s+",
    r"mkfs",
    r"dd\s+if=/dev/zero",
    r":\(\)\s*\{.*\|.*&.*\};:",  # fork bomb
    r">\s*/dev/sd[a-z]",
    r">\s*/dev/null\s*>\s*/dev/null",  # redirect both stdin and stdout to null
    r"eval\s*\(",
    r"exec\s*\(",
    r"system\s*\(",
    r" subprocess",  # python subprocess in a shell string
    r"__import__",  # python code injection
    r"open\s*\([^)]*\.py",  # file write to .py
    # System paths that should never be modified by rm/chmod/mv
    r"rm\s+.*\.pyenv",
    r"rm\s+.*/shims/",
    r"rm\s+.*/\.(ssh|gnupg|aws|docker|kube|config)/",
    r"chmod\s+777\s+/",
    r"chown\s+-R\s+/",
]

# Shell metacharacters that are always blocked (command chaining, piping, substitution).
# NOTE: && and || are intentionally NOT blocked — they're common in legitimate
# fallback patterns like "which X || pip show X" or "cd dir && make".
# The dangerous command patterns (rm, sudo, etc.) are checked separately.
_SHELL_METACHARS = {";", "`", "$("}


def _has_unsafe_pipe(command: str) -> bool:
    """Check for standalone pipe (|) not part of ||."""
    # Remove all || first, then check for remaining |
    cleaned = command.replace("||", "")
    return "|" in cleaned


class ExecuteCommandTool(BaseTool):
    user_facing_name = "Bash"
    is_destructive = True

    name = "execute_command"
    description = "Execute a shell command and return the output"

    def __init__(self, allowed_commands: Optional[List[str]] = None):
        self.allowed_commands = allowed_commands

    def check_permissions(self, args: dict) -> Tuple[bool, str]:
        """Validate command before execution — checked by engine before calling execute()."""
        command = args.get("command", "")
        if error := self._validate_command(command):
            return False, error
        return True, ""

    def render_call(self, args: dict) -> str:
        cmd = args.get("command", "")
        # Short form for display: just the first meaningful word + brief
        parts = cmd.split()
        if len(parts) <= 3:
            return cmd[:76]
        return " ".join(parts[:3]) + "..."

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.metadata:
            lines = result.metadata.get("lines", 0)
            duration_ms = result.metadata.get("duration_ms", 0)
            if duration_ms > 0:
                return f"Bash · {duration_ms/1000:.1f}s · {lines} lines"
            return f"Bash · {lines} lines"
        return super().render_result(result)

    def _validate_command(self, command: str) -> Optional[str]:
        """Validate command using shlex.split and pattern matching.

        Returns error message if blocked, None if allowed.
        """
        # Check blocked patterns first
        cmd_lower = command.lower()
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, cmd_lower, re.IGNORECASE):
                return f"Blocked pattern '{pattern}' in command"

        # Block shell metacharacters that enable command chaining
        for mc in _SHELL_METACHARS:
            if mc in command:
                return f"Shell metacharacter '{mc}' not allowed (use create_subprocess_exec-style argument lists)"
        # Check standalone pipe (| not part of ||) — only block in dangerous contexts
        if _has_unsafe_pipe(command):
            dangerous_with_pipe = [
                "curl",
                "wget",
                "ssh",
                "nc ",
                "telnet",
                "bash ",
                "sh ",
                "sudo",
                "rm ",
                "dd ",
                "/dev/",
                "> /",
                "chmod",
                "chown",
            ]
            if any(op in cmd_lower for op in dangerous_with_pipe):
                return f"Shell metacharacter '|' not allowed with '{cmd_lower[:20]}...'"

        # Use shlex.split for proper parsing
        try:
            parts = shlex.split(command)
        except ValueError as e:
            return f"Command parse error: {e}"

        if not parts:
            return "Empty command"

        # Get the base command (resolve symlinks to prevent bypass)
        cmd_path = Path(parts[0])
        cmd_name = cmd_path.name if cmd_path.is_absolute() else cmd_path.name

        # Whitelist check
        effective_allowed = (
            self.allowed_commands if self.allowed_commands is not None else SAFE_COMMANDS
        )
        if effective_allowed and cmd_name not in effective_allowed:
            return f"Command not allowed: {cmd_name}"

        # Additional check: if using a path like /usr/bin/python, verify it
        if cmd_path.is_absolute():
            # Resolve and check if in an allowed location
            try:
                resolved = cmd_path.resolve()
                # Block access to sensitive paths
                sensitive = ["/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root/.ssh"]
                for s in sensitive:
                    if str(resolved).startswith(s):
                        return f"Access to sensitive path blocked: {s}"
            except Exception:
                pass

        return None

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 30,
        max_wait_seconds: int = 0,
        **kwargs,
    ) -> ToolResult:
        """Execute a shell command.

        Args:
            command: shell command to run.
            cwd: working directory.
            timeout: idle timeout in seconds (kill if NO output for this long).
                Default 30s. Catches hung/silent processes (deadlocks,
                network stalls). Processes that produce output continuously
                are unaffected — a build/test that streams progress for
                10 minutes will run to completion as long as something
                keeps coming out.
            max_wait_seconds: OPT-IN wall-clock cap. Default 0 (disabled).
                Pass a positive value to kill the process once total
                elapsed time exceeds it, regardless of output activity.
                Useful for bounding genuinely-bad cases (a hung CI run,
                a runaway loop) when you know the legitimate upper bound.
                Leave at 0 to trust that idle_timeout is sufficient.

        Why opt-in: commands that produce output continuously are NOT
        unresponsive — `pytest` reporting progress, `cargo build` ticking
        test counts, a SQL query streaming rows — all legitimate uses
        that should be allowed to run as long as needed. Killing them by
        wall-clock was a regression for users with slow-but-honest
        workloads.

        For dev servers / watch processes (npm run dev, python -m
        http.server) that emit brief startup output then go silent,
        the idle timeout WILL fire. Run those externally or via
        `nohup ... &`.
        """

        # Security validation
        if error := self._validate_command(command):
            return ToolResult(
                success=False,
                content="",
                error=f"Command blocked: {error}",
                metadata={"blocked": True, "lines": 0, "duration_ms": 0},
            )

        try:
            t0 = time.time()
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # read_process handles the idle timeout internally. The
            # optional max_wait_seconds adds a wall-clock cap when the
            # caller explicitly opts in (default 0 = disabled).
            try:
                if max_wait_seconds > 0:
                    stdout, stderr = await asyncio.wait_for(
                        read_process(proc, idle_timeout=timeout),
                        timeout=max_wait_seconds,
                    )
                else:
                    stdout, stderr = await read_process(proc, idle_timeout=timeout)
            except asyncio.TimeoutError:
                # asyncio.wait_for raises TimeoutError when the
                # wall-clock cap is exceeded (NOT a subprocess idle
                # timeout — that's IdleTimeoutError). Kill and report.
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                elapsed = int((time.time() - t0))
                return ToolResult(
                    success=False,
                    content="",
                    error=(
                        f"Wall-clock timeout after {max_wait_seconds}s — process killed "
                        f"(idle timeout {timeout}s never fired, meaning the process kept "
                        f"emitting output but never exited). If this is intentional, "
                        f"increase max_wait_seconds or run detached with `nohup ... &`."
                    ),
                    metadata={"duration_ms": elapsed * 1000, "lines": 0, "killed": True},
                )

            elapsed_ms = int((time.time() - t0) * 1000)
            output_lines = len(stdout.splitlines()) if stdout else 0

            if proc.returncode != 0:
                return ToolResult(
                    success=False,
                    content=stdout,
                    error=stderr or f"Exit code: {proc.returncode}",
                    metadata={"duration_ms": elapsed_ms, "lines": output_lines},
                )

            return ToolResult(
                success=True,
                content=stdout,
                metadata={"duration_ms": elapsed_ms, "lines": output_lines},
            )

        except IdleTimeoutError as e:
            partial = e.stdout.strip()
            msg = f"No output for {timeout}s — process killed"
            if partial:
                msg += f"\n[partial output before timeout]\n{partial[-500:]}"
            return ToolResult(success=False, content=e.stdout, error=msg)
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "command": {"type": "string", "description": "Shell command to execute"},
                        "cwd": {"type": "string", "description": "Working directory"},
                        "timeout": {
                            "type": "integer",
                            "description": "Idle timeout in seconds (kill if no output for this long)",
                            "default": 30,
                        },
                        "max_wait_seconds": {
                            "type": "integer",
                            "description": (
                                "OPT-IN wall-clock cap. Default 0 (disabled). Pass a positive "
                                "value to kill the process after this many seconds regardless "
                                "of output. Use only when you know the legitimate upper bound "
                                "and want to bound genuinely-bad cases. Commands with continuous "
                                "output (pytest, cargo build) are NOT killed unless this is set."
                            ),
                            "default": 0,
                        },
                    },
                    "required": ["command"],
                },
            },
        }


# Register tool with safe defaults
registry.register(ExecuteCommandTool())
