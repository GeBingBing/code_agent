"""Shared workspace root resolution — single source of truth.

Reads `CODING_AGENT_WORKSPACE` from the config (env var or
``~/.coding-agent/config.json``). If unset, falls back to the current
working directory. No path is hardcoded — every consumer imports
``WORKSPACE_ROOT`` (or calls ``get_workspace_root()``) from here.

For sub-agent worktree isolation, call ``set_workspace_root(path)`` to
override the root for the current asyncio task; ``current_workspace()``
returns the effective root (task-local override if set, else the global).
"""

import contextvars
from pathlib import Path

from .config import config


def get_workspace_root() -> Path:
    """Resolve the workspace root from config/env, falling back to cwd."""
    raw = config.get("workspace")
    return Path(raw).resolve() if raw else Path.cwd().resolve()


# Resolved at import time, matching the existing pattern in engine.py.
WORKSPACE_ROOT: Path = get_workspace_root()

# Task-local override so a sub-agent running in a git worktree can redirect
# file operations without touching the global (which the parent agent uses).
_workspace_var: contextvars.ContextVar = contextvars.ContextVar(
    "_workspace_root_override", default=None
)


def set_workspace_root(path) -> contextvars.Token:
    """Override the workspace root for the current asyncio task.

    Returns a token the caller passes to reset_workspace_root() to restore
    the previous value (wrap the sub-agent run in try/finally).
    """
    return _workspace_var.set(Path(path).resolve())


def reset_workspace_root(token: contextvars.Token) -> None:
    """Restore the workspace root after a set_workspace_root() call."""
    _workspace_var.reset(token)


def current_workspace() -> Path:
    """The effective workspace root: task-local override if set, else global."""
    override = _workspace_var.get()
    return override if override is not None else WORKSPACE_ROOT
