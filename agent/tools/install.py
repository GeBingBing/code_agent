"""Package installation tool — unified interface for system and language package managers.

Supports: brew, apt, dnf, yum, pacman, pip, npm, yarn, pnpm, gem, cargo, poetry, conda.
"""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry, read_process, IdleTimeoutError
from ..core.workspace import get_workspace_root


def _workspace() -> Path:
    """Re-resolve on each call so monkeypatched env vars take effect."""
    return get_workspace_root()


def _detect_package_manager(package: str, manager: str) -> str:
    """Auto-detect the best package manager for a given package name.

    Heuristics:
    - If manager is explicitly set (not "auto"), use it directly
    - Check project files: requirements.txt, pyproject.toml, package.json, etc.
    - Check available system package managers
    - Fall back by platform
    """
    if manager != "auto":
        return manager

    ws = _workspace()

    # Check project files for language ecosystem hints
    if (ws / "pyproject.toml").exists():
        if (ws / "poetry.lock").exists():
            return "poetry add"
        return "pip install"
    if (ws / "requirements.txt").exists():
        return "pip install"
    if (ws / "Pipfile").exists():
        return "pipenv install"
    if (ws / "package.json").exists():
        if (ws / "yarn.lock").exists():
            return "yarn add"
        if (ws / "pnpm-lock.yaml").exists():
            return "pnpm add"
        return "npm install"
    if (ws / "Gemfile").exists():
        return "bundle add"
    if (ws / "Cargo.toml").exists():
        return "cargo install"

    # Fall back: check available system package managers
    if shutil.which("brew"):
        return "brew install"
    if shutil.which("apt"):
        return "apt install -y"
    if shutil.which("apt-get"):
        return "apt-get install -y"
    if shutil.which("dnf"):
        return "dnf install -y"
    if shutil.which("yum"):
        return "yum install -y"
    if shutil.which("pacman"):
        return "pacman -S --noconfirm"
    if shutil.which("conda"):
        return "conda install -y"
    if shutil.which("pip"):
        return "pip install"

    # Last resort
    return "pip install"


async def _run_install(cmd_parts: list, cwd: str, timeout: int = 120) -> tuple:
    """Run an install command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd_parts,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await read_process(proc, idle_timeout=timeout)
    except IdleTimeoutError as e:
        proc.kill()
        await proc.wait()
        return e.stdout, f"No output for {timeout}s — process killed\n[partial output]\n{e.stdout[-500:]}", -1

    return (stdout, stderr, proc.returncode)


class InstallPackageTool(BaseTool):
    user_facing_name = "Install"

    """Install a package using the appropriate package manager.

    Auto-detects the best package manager based on the project context
    and available system tools.
    """

    name = "install_package"
    description = (
        "Install a package using the appropriate package manager. "
        "Auto-detects from pip/npm/brew/apt/cargo/etc based on project files "
        "and available system tools. Set manager to force a specific one "
        "(e.g. 'pip install', 'npm install', 'brew install')."
    )

    @property
    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name to install (e.g. 'requests', 'express', 'ripgrep')",
                    },
                    "manager": {
                        "type": "string",
                        "description": "DO NOT set this unless install_package chose the wrong manager. 'auto' auto-detects from project files (pyproject.toml→pip, package.json→npm, etc.)",
                        "default": "auto",
                    },
                    "args": {
                        "type": "string",
                        "description": "Additional arguments to pass to the install command (e.g. '--upgrade', '--save-dev')",
                        "default": "",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (defaults to workspace root)",
                        "default": "",
                    },
                },
                "required": ["package"],
            },
        }

    # Common package suffixes for smart retry when a bare name fails.
    # e.g. if "hermes" fails, try "hermes-agent", "hermes-cli", "hermes-sdk" etc.
    _SMART_SUFFIXES = [
        "-agent", "-cli", "-tool", "-server", "-client",
        "-sdk", "-lib", "-python", "-js", "-go", "-rs",
    ]

    async def execute(
        self,
        package: str,
        manager: str = "auto",
        args: str = "",
        cwd: str = "",
        **kwargs,
    ) -> ToolResult:
        """Install a package.

        Args:
            package: Package name to install (e.g. 'requests', 'express', 'ripgrep')
            manager: Package manager to use. "auto" (default) auto-detects.
                     Examples: "pip install", "npm install", "brew install",
                     "apt install -y", "cargo install"
            args: Additional arguments to pass to the install command
            cwd: Working directory (defaults to workspace root)
        """
        # Normalize package name: "hermes agent" → "hermes-agent"
        # Package managers (pip, npm, apt, brew) use hyphens, not spaces
        if " " in package:
            package = package.replace(" ", "-")

        work_dir = cwd or str(_workspace())

        # Resolve the package manager command
        manager_cmd = _detect_package_manager(package, manager)

        result = await self._try_install(package, manager_cmd, args, work_dir)
        if result.success:
            return result

        # ── First attempt failed — try smart package name correction ──
        # If the package name has no hyphen (e.g. "hermes"), the user might
        # have meant "hermes-agent", "hermes-cli", etc. Try common suffixes.
        if "-" not in package[1:] and manager_cmd in ("pip install", "pip3 install", "npm install", "brew install"):
            manager_cmd = _detect_package_manager(package, manager)
            # Re-read project hints to pick the right suffixes
            ws = _workspace()
            suffixes = list(self._SMART_SUFFIXES)
            if (ws / "package.json").exists():
                # Prefer JS/TS suffixes
                suffixes = ["-cli", "-tool", "-agent", "-server", "-client", "-sdk", "-js"] + suffixes
            elif (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
                # Prefer Python suffixes
                suffixes = ["-agent", "-cli", "-tool", "-server", "-client", "-sdk", "-python"] + suffixes

            for suffix in suffixes:
                if suffix in package:
                    continue  # Already contains this suffix
                candidate = package + suffix
                retry_result = await self._try_install(candidate, manager_cmd, args, work_dir)
                if retry_result.success:
                    return retry_result

        # ── All attempts failed ──
        # Clean up stderr: remove pip version notices that mask real errors
        import re as _re
        clean_stderr = result.error or ""
        if clean_stderr:
            clean_stderr = _re.sub(r'\[notice\].*\n?', '', clean_stderr)
            clean_stderr = clean_stderr.strip()

        # Build a helpful error that guides the LLM toward the right fix
        if "-" not in package[1:]:
            hint = f"Package '{package}' not found. If the user asked for '{package}-agent' or '{package}-cli', retry install_package with the full name including the suffix (e.g. package='{package}-agent')."
        else:
            hint = f"Package '{package}' not found. Check the package name and try again."

        return ToolResult(
            success=False,
            content=result.content or "",
            error=f"Failed to install {package} (exit code 1): {clean_stderr}" if clean_stderr else hint,
        )

    async def _try_install(self, package: str, manager_cmd: str, args: str, work_dir: str) -> ToolResult:
        """Run a single install attempt. Returns ToolResult."""
        parts = manager_cmd.split() + [package]
        if args:
            parts.extend(args.split())

        stdout, stderr, rc = await _run_install(parts, work_dir)

        if rc == 0:
            import re
            version = ""
            pkg_norm = re.escape(package).replace(r'\-', r'[-_]')
            for pattern in [
                r'Successfully installed\s+' + pkg_norm + r'-([\d.]+)',
                r'Requirement already satisfied:\s+' + pkg_norm + r'\s+in\s+\S+\s+\(([\d.]+)\)',
            ]:
                vm = re.search(pattern, stdout)
                if vm:
                    version = vm.group(1)
                    break
            content = f"Installed {package}"
            if version:
                content += f" v{version}"
            content += f" via {manager_cmd}"
            if stdout:
                content += f"\n{stdout[-500:]}"
            return ToolResult(success=True, content=content)
        else:
            return ToolResult(
                success=False,
                content=stdout or "",
                error=f"Failed to install {package} (exit code {rc}): {stderr[-500:]}" if stderr else f"Failed to install {package} (exit code {rc})",
            )


class UninstallPackageTool(BaseTool):
    user_facing_name = "Uninstall"

    """Uninstall a package using the appropriate package manager.

    Auto-detects pip/npm/brew based on project context. For pip, uses
    "pip uninstall -y <pkg>". For npm, uses "npm uninstall <pkg>".
    """

    name = "uninstall_package"
    description = (
        "Uninstall a package using the appropriate package manager. "
        "Auto-detects pip/npm/brew. Supports pip (pip uninstall -y), "
        "npm (npm uninstall), brew (brew uninstall), and apt (apt remove -y)."
    )

    @property
    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name to uninstall (e.g. 'hermes-agent', 'express')",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (defaults to workspace root)",
                        "default": "",
                    },
                },
                "required": ["package"],
            },
        }

    async def execute(self, package: str, cwd: str = "", **kwargs) -> ToolResult:
        """Uninstall a package."""
        # Normalize package name
        if " " in package:
            package = package.replace(" ", "-")

        work_dir = cwd or str(_workspace())

        # Detect package manager and build uninstall command
        manager_cmd = _detect_package_manager(package, "auto")

        # Map install commands to uninstall commands
        if manager_cmd.startswith("pip"):
            parts = ["pip", "uninstall", "-y", package]
        elif manager_cmd.startswith("npm"):
            parts = ["npm", "uninstall", package]
        elif manager_cmd.startswith("yarn"):
            parts = ["yarn", "remove", package]
        elif manager_cmd.startswith("pnpm"):
            parts = ["pnpm", "remove", package]
        elif manager_cmd.startswith("brew"):
            parts = ["brew", "uninstall", package]
        elif manager_cmd.startswith("apt"):
            parts = ["apt", "remove", "-y", package]
        elif manager_cmd.startswith("apt-get"):
            parts = ["apt-get", "remove", "-y", package]
        elif manager_cmd.startswith("cargo"):
            parts = ["cargo", "uninstall", package]
        else:
            # Fallback: pip uninstall
            parts = ["pip", "uninstall", "-y", package]

        stdout, stderr, rc = await _run_install(parts, work_dir)

        if rc == 0:
            content = f"Uninstalled {package} via {' '.join(parts)}"
            if stdout:
                content += f"\n{stdout[-500:]}"
            return ToolResult(success=True, content=content)
        else:
            return ToolResult(
                success=False,
                content=stdout or "",
                error=f"Failed to uninstall {package} (exit code {rc}): {stderr[-300:]}" if stderr
                else f"Failed to uninstall {package} (exit code {rc})",
            )


# Register
registry.register(InstallPackageTool())
registry.register(UninstallPackageTool())
