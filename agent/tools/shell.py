"""Shell execution tool"""

import asyncio
import re
import shlex
import time
from pathlib import Path
from typing import Optional, List, Tuple

from .base import BaseTool, ToolResult, registry, read_process, IdleTimeoutError


# Whitelist of safe commands
SAFE_COMMANDS = {
    "ls", "cat", "grep", "find", "echo", "python", "python3", "node", "npm",
    "pip", "pip3", "git", "cd", "pwd", "mkdir", "rm", "cp", "mv", "touch",
    "chmod", "head", "tail", "wc", "sort", "uniq", "awk", "sed", "cut",
    "xargs", "which", "file", "stat", "diff", "patch", "make", "cmake",
    "cargo", "rustc", "go", "java", "javac", "node", "ruby", "perl",
    "bash", "sh", "zsh", "fish", "curl", "wget", "tar", "gzip", "gunzip",
    "zip", "unzip", "rsync", "scp", "ssh", "ping", "nc", "netstat",
    "ps", "top", "htop", "kill", "killall", "pkill", "pgrep", "free",
    "df", "du", "lsblk", "mount", "umount", "fdisk", "mkfs.ext4",
    # Package managers
    "brew", "apt", "apt-get", "dnf", "yum", "pacman", "conda", "gem",
    "snap", "npx", "pnpm", "yarn", "poetry", "mamba", "pipx",
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
    r"__import__",    # python code injection
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
                "curl", "wget", "ssh", "nc ", "telnet", "bash ", "sh ",
                "sudo", "rm ", "dd ", "/dev/", "> /", "chmod", "chown",
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
        effective_allowed = self.allowed_commands if self.allowed_commands is not None else SAFE_COMMANDS
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

    async def execute(self, command: str, cwd: Optional[str] = None, timeout: int = 30, **kwargs) -> ToolResult:
        """Execute a shell command"""

        # Security validation
        if error := self._validate_command(command):
            return ToolResult(
                success=False,
                content="",
                error=f"Command blocked: {error}"
            )

        try:
            t0 = time.time()
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await read_process(proc, idle_timeout=timeout)
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
                success=True, content=stdout,
                metadata={"duration_ms": elapsed_ms, "lines": output_lines},
            )

        except IdleTimeoutError as e:
            partial = e.stdout.strip()
            msg = f"No output for {timeout}s — process killed"
            if partial:
                msg += f"\n[partial output before timeout]\n{partial[-500:]}"
            return ToolResult(success=False, content=e.stdout, error=msg)
        except asyncio.TimeoutError:
            return ToolResult(success=False, content="", error=f"No output for {timeout}s (idle timeout, process killed)")
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
                        "timeout": {"type": "integer", "description": "Idle timeout in seconds (kill if no output for this long)", "default": 30}
                    },
                    "required": ["command"]
                },
            },
        }


# Register tool with safe defaults
registry.register(ExecuteCommandTool())