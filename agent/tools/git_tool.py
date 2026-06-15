"""Git tool - repository operations for the coding agent.

Rollback defense (added 2026-06-15)
-----------------------------------
Before this revision the tool allowed ``checkout`` and ``stash`` outright.
Both are capable of silently destroying the user's manual edits:

  * ``git checkout -- <path>`` restores ``<path>`` to its committed state,
    wiping any uncommitted manual change to that file.
  * ``git checkout <commit> -- <path>`` does the same but from a specific
    commit — same effect for the user.
  * ``git stash`` moves all uncommitted changes off the working tree into
    the stash list. If never popped they become invisible noise.

Branch switching (``git checkout <branch>``, ``git checkout -b <new>``) is
ALLOWED — it's a normal flow that the user can recover from by switching
back, and it doesn't destroy uncommitted work in the way the ``-- <path>``
form does.

We detect the file-restore form by scanning for the literal ``--`` token
that separates the refspec from the path. Branch forms never contain ``--``.
"""

import asyncio
import shlex
from typing import Optional

from .base import BaseTool, ToolResult, registry


class GitTool(BaseTool):
    user_facing_name = "Git"

    name = "git"
    description = "Execute git commands in the workspace repository"

    # Subcommands that explicitly can clobber uncommitted work and are blocked.
    _BLOCKED_SUBCOMMANDS = frozenset({"stash"})

    async def execute(self, command: str, cwd: Optional[str] = None, **kwargs) -> ToolResult:
        """Execute a git command.

        Args:
            command: The git subcommand and arguments (e.g. "status", "diff", "commit -m 'msg'")
            cwd: Working directory for the git command
        """
        allowed_subcommands = {
            "status",
            "diff",
            "log",
            "show",
            "branch",
            "remote",
            "config",
            "add",
            "commit",
            "push",
            "pull",
            "fetch",
            "merge",
            "rebase",
            "checkout",
            "switch",
            "stash",
            "tag",
            "clone",
            "init",
        }

        # Block shell metacharacters that enable command chaining
        for mc in ("&&", "||", ";", "|", "`", "$("):
            if mc in command:
                return ToolResult(
                    success=False,
                    content="",
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

        # Block subcommands that can clobber uncommitted work (stash, etc.)
        # Must come AFTER the allowlist check so the user sees the specific
        # "blocked because rollback" message instead of a generic
        # "not allowed" message.
        if subcommand in self._BLOCKED_SUBCOMMANDS:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Git subcommand '{subcommand}' is blocked: it can hide "
                    f"uncommitted manual edits. Run it directly in your shell "
                    f"if you really need it."
                ),
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

        # Block checkout file-restore forms: anything containing `--` is the
        # refspec/path separator in checkout and means "restore files". Branch
        # switching forms (e.g. `checkout main`, `checkout -b feat/x`) never
        # contain `--`.
        if subcommand == "checkout" and "--" in cmd_parts[1:]:
            return ToolResult(
                success=False,
                content="",
                error=(
                    "'git checkout -- <path>' restores files to their committed "
                    "state, which would silently overwrite your manual edits. "
                    "Use branch switching (`git checkout <branch>` or "
                    "`git checkout -b <new>`) instead, or restore files "
                    "manually in your shell."
                ),
            )

        full_command = f"git {command}"

        try:
            proc = await asyncio.create_subprocess_shell(
                full_command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

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
