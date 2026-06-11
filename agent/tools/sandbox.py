"""Sandbox tool - execute commands in Docker with snapshot/rollback."""

import asyncio
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry


class Sandbox:
    """Docker-based sandbox with filesystem snapshots."""

    def __init__(self, image: str = "python:3.11-slim"):
        self.image = image
        self._has_docker: Optional[bool] = None  # Lazy-check to avoid asyncio.run in __init__
        self.snapshots: dict[str, str] = {}

    @property
    def has_docker(self) -> bool:
        """Backward-compat: return cached result or False if not yet checked."""
        return self._has_docker if self._has_docker is not None else False

    @has_docker.setter
    def has_docker(self, value: bool):
        self._has_docker = value

    async def _check_docker(self) -> bool:
        """Check if Docker is available (async-safe)."""
        if self._has_docker is not None:
            return self._has_docker
        try:
            proc = await asyncio.create_subprocess_shell(
                "docker --version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            self._has_docker = proc.returncode == 0
        except Exception:
            self._has_docker = False
        return self._has_docker

    def snapshot(self, source_dir: str, name: str) -> str:
        """Create a snapshot of a directory. Returns snapshot path."""
        snap_dir = tempfile.mkdtemp(prefix=f"sandbox_{name}_")
        shutil.copytree(source_dir, snap_dir, dirs_exist_ok=True)
        self.snapshots[name] = snap_dir
        return snap_dir

    def rollback(self, name: str, target_dir: str):
        """Restore a snapshot to target directory."""
        snap_dir = self.snapshots.get(name)
        if not snap_dir:
            raise ValueError(f"No snapshot named '{name}'")
        if Path(target_dir).exists():
            shutil.rmtree(target_dir)
        shutil.copytree(snap_dir, target_dir)

    async def execute(self, command: str, cwd: str, timeout: int = 60) -> dict:
        """Execute a command in Docker or fallback to local."""
        if not await self._check_docker():
            return {
                "success": False,
                "stdout": "",
                "stderr": "Docker not available. Install Docker to enable sandbox mode.",
            }

        abs_cwd = str(Path(cwd).resolve())
        try:
            proc = await asyncio.create_subprocess_shell(
                f"docker run --rm -v {shlex.quote(abs_cwd)}:/workspace -w /workspace {self.image} sh -c {shlex.quote(command)}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else "",
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Timeout after {timeout}s",
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
            }


class SandboxExecuteTool(BaseTool):
    user_facing_name = "Sandbox"
    is_destructive = True

    name = "sandbox_execute"
    description = "Execute a command in an isolated Docker sandbox"

    def __init__(self):
        self.sandbox = Sandbox()

    async def execute(self, command: str, cwd: str = ".", timeout: int = 60, **kwargs) -> ToolResult:
        try:
            result = await self.sandbox.execute(command, cwd, timeout)
            output = result["stdout"]
            if result["stderr"]:
                output += f"\n[stderr]\n{result['stderr']}"
            return ToolResult(
                success=result["success"],
                content=output,
                error=result["stderr"] if not result["success"] else None,
            )
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
                        "command": {"type": "string", "description": "Shell command to run in sandbox"},
                        "cwd": {"type": "string", "description": "Working directory to mount", "default": "."},
                        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60},
                    },
                    "required": ["command"],
                },
            },
        }


# Shared sandbox instance for snapshot/rollback
_shared_sandbox = Sandbox()


class SnapshotTool(BaseTool):
    user_facing_name = "Snapshot"

    name = "snapshot"
    description = "Create a filesystem snapshot before risky operations"

    def __init__(self):
        self.sandbox = _shared_sandbox

    async def execute(self, path: str = ".", name: str = "default", **kwargs) -> ToolResult:
        try:
            snap_path = self.sandbox.snapshot(path, name)
            return ToolResult(success=True, content=f"Snapshot '{name}' created at {snap_path}")
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
                        "path": {"type": "string", "description": "Directory to snapshot", "default": "."},
                        "name": {"type": "string", "description": "Snapshot name", "default": "default"},
                    },
                    "required": ["path"],
                },
            },
        }


class RollbackTool(BaseTool):
    user_facing_name = "Rollback"
    is_destructive = True

    name = "rollback"
    description = "Restore a directory from a snapshot"

    def __init__(self):
        self.sandbox = _shared_sandbox

    async def execute(self, name: str, target: str = ".", **kwargs) -> ToolResult:
        try:
            self.sandbox.rollback(name, target)
            return ToolResult(success=True, content=f"Rolled back '{target}' from snapshot '{name}'")
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
                        "name": {"type": "string", "description": "Snapshot name to restore"},
                        "target": {"type": "string", "description": "Directory to restore into", "default": "."},
                    },
                    "required": ["name"],
                },
            },
        }


# Register tools
registry.register(SandboxExecuteTool())
registry.register(SnapshotTool())
registry.register(RollbackTool())
