"""Git tool - repository operations for the coding agent."""

import asyncio
import shlex
import subprocess
from typing import Optional

from .base import BaseTool, ToolResult, registry


class GitTool(BaseTool):
    user_facing_name = "Git"

    name = "git"
    description = "Execute git commands in the workspace repository"

    async def execute(self, command: str, cwd: Optional[str] = None, **kwargs) -> ToolResult:
        """Execute a git command.

        Args:
            command: The git subcommand and arguments (e.g. "status", "diff", "commit -m 'msg'")
            cwd: Working directory for the git command
        """
        allowed_subcommands = {
            "status", "diff", "log", "show", "branch", "remote", "config",
            "add", "commit", "push", "pull", "fetch", "merge", "rebase",
            "checkout", "switch", "stash", "tag", "clone", "init",
        }

        # Block shell metacharacters that enable command chaining
        for mc in ("&&", "||", ";", "|", "`", "$("):
            if mc in command:
                return ToolResult(
                    success=False, content="",
                    error=f"Shell metacharacter '{mc}' not allowed in git command",
                )

        cmd_parts = shlex.split(command)
        if not cmd_parts:
            return ToolResult(success=False, content="", error="Empty git command")

        subcommand = cmd_parts[0]
        if subcommand not in allowed_subcommands:
            return ToolResult(
                success=False,
                content="",
                error=f"Git subcommand '{subcommand}' is not allowed. Allowed: {', '.join(sorted(allowed_subcommands))}",
            )

        # Block dangerous flags (check individual args, not substring)
        dangerous_flags = {"--force", "-f", "--hard"}
        for part in cmd_parts[1:]:
            if part in dangerous_flags:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Dangerous flag '{part}' is not allowed in git commands",
                )

        full_command = f"git {command}"

        try:
            proc = await asyncio.create_subprocess_shell(
                full_command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=120
            )

            output = stdout.decode("utf-8", errors="replace")
            error_output = stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return ToolResult(
                    success=False,
                    content=output,
                    error=error_output or f"Git exited with code {proc.returncode}",
                )

            return ToolResult(success=True, content=output or "(no output)")

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
                        "command": {
                            "type": "string",
                            "description": "Git subcommand and arguments, e.g. 'status', 'diff', 'commit -m \"msg\"'",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory for the git command",
                        },
                    },
                    "required": ["command"],
                },
            },
        }


# Register tool
registry.register(GitTool())
