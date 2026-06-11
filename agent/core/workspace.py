"""Shared workspace root resolution — single source of truth.

Reads `CODING_AGENT_WORKSPACE` from the config (env var or
`~/.coding-agent/config.json`). If unset, falls back to the current
working directory. No path is hardcoded — every consumer imports
`WORKSPACE_ROOT` (or calls `get_workspace_root()`) from here.
"""

from pathlib import Path

from .config import config


def get_workspace_root() -> Path:
    """Resolve the workspace root from config/env, falling back to cwd."""
    raw = config.get("workspace")
    return Path(raw).resolve() if raw else Path.cwd().resolve()


# Resolved at import time, matching the existing pattern in engine.py.
WORKSPACE_ROOT: Path = get_workspace_root()
