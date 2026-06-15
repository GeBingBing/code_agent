"""Detect & gate writes to the agent's own source tree.

Why this module exists
----------------------
Before 2026-06-15 the agent's file-write tools (write_file, apply_diff,
replace_lines, insert_after_line) only enforced `_is_within_workspace()`. When
a user runs the agent from the repo root (the common case), `WORKSPACE_ROOT`
resolves to the project root — meaning the tools had the implicit right to
overwrite the agent's own source code (`agent/`, `ui/`, `index/`, etc.).

In practice this caused the "改动被回滚" pain: a user manually edits
`agent/llm/client.py`, then runs the agent on a routine task, and the agent's
own ``write_file``/``apply_diff`` silently overwrites the manual change. The
agent's reasoning is correct ("the LLM decided the file needed different
content"); the user-visible symptom is "my edit disappeared".

Defense
-------
* Detect "is this workspace the agent's own source tree?" by checking for
  ``pyproject.toml`` + ``agent/`` at ``WORKSPACE_ROOT``. Only when BOTH are
  present do we treat the workspace as the source tree (i.e. self-evolution
  scenario). A standalone ``workspace/`` subdirectory does NOT trip this.
* In that case, deny writes to the top-level source dirs ``agent/``,
  ``ui/``, ``index/``, ``tests/``, ``docs/`` and a handful of root-level
  config files (``pyproject.toml``, ``CLAUDE.md``, ``CODING_AGENT.md``).
* Provide an escape hatch via env var ``CODING_AGENT_ALLOW_SELF_MODIFY=1``
  for explicit self-evolution flows (e.g. an orchestrator step that
  genuinely intends to edit the agent). Default is DENY — fail-closed.

Why env-var opt-out (not opt-in)
--------------------------------
Default-deny matches the user's mental model: "I edited code in my IDE, the
agent must not touch it." The opt-out is one env-var so self-evolution
remains a deliberate, observable action (visible in process env, in audit
log, in `ps`).

Why fail-closed via ToolResult (not exception)
----------------------------------------------
A raised exception inside a tool would crash the ReAct loop and produce a
confusing traceback the user didn't trigger. Returning ``ToolResult(success=
False, error=...)`` keeps the loop alive and surfaces a clear, actionable
error in the LLM context: "this path is protected; if you really need to
edit it, ask the user to set CODING_AGENT_ALLOW_SELF_MODIFY=1".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Top-level directories that count as "agent's own source" when WORKSPACE_ROOT
# is the project root. Matched case-sensitively (project dirs are lowercase).
_SELF_SOURCE_DIRS: tuple[str, ...] = (
    "agent",
    "ui",
    "index",
)

# Top-level dirs that are also protected when running inside the source tree
# (tests, docs — the agent should not silently rewrite documentation or
# test files the user maintains by hand).
_SELF_AUX_DIRS: tuple[str, ...] = (
    "tests",
    "docs",
)

# Root-level files that are protected (project config, agent instructions).
# These are matched by exact filename at WORKSPACE_ROOT, not by suffix.
_SELF_ROOT_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "CLAUDE.md",
    "CODING_AGENT.md",
)

# Heuristic: this is "the agent's own source tree" iff BOTH a project marker
# file and the agent/ package directory exist at WORKSPACE_ROOT.
_PROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
)


def is_self_source_workspace(workspace_root: Path) -> bool:
    """True if ``workspace_root`` is the agent's own source tree.

    Detection rules:
      * `pyproject.toml` or `setup.py` exists at the root
      * `agent/` directory exists at the root (the agent package itself)

    We require BOTH because a bare `pyproject.toml` could belong to any
    Python project the user is working on — only when ``agent/`` is also
    present is it the agent's own source tree.
    """
    if not workspace_root.is_dir():
        return False
    has_marker = any((workspace_root / m).exists() for m in _PROJECT_MARKERS)
    has_agent_pkg = (workspace_root / "agent").is_dir()
    return has_marker and has_agent_pkg


def _resolve(path: str, workspace_root: Path) -> Optional[Path]:
    """Resolve ``path`` against ``workspace_root`` if not absolute.

    Returns None if the path can't be resolved (defensive — caller treats
    that as "not inside the workspace, not protected by us").
    """
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = workspace_root / p
        return p.resolve()
    except (OSError, RuntimeError):
        return None


def _first_segment(rel: Path) -> Optional[str]:
    """First path segment of a relative path, or None if rel is at root."""
    parts = rel.parts
    if not parts or parts[0] in ("", "."):
        return None
    return parts[0]


def is_protected_path(path: str, workspace_root: Path) -> bool:
    """True if writing to ``path`` would touch the agent's own source tree.

    Args:
        path: The path the LLM passed to a write tool (relative or absolute).
        workspace_root: The resolved workspace root.

    Returns:
        True if the write should be DENIED. False if the write is safe to
        proceed (or workspace_root isn't the agent's own source tree).
    """
    # Opt-out: explicit self-evolution intent — caller already opted in.
    if os.getenv("CODING_AGENT_ALLOW_SELF_MODIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False

    # If this workspace isn't the agent's own source, there's nothing to protect.
    if not is_self_source_workspace(workspace_root):
        return False

    resolved = _resolve(path, workspace_root)
    if resolved is None:
        return False

    try:
        rel = resolved.relative_to(workspace_root.resolve())
    except ValueError:
        # Path lives outside WORKSPACE_ROOT — already gated by
        # _is_within_workspace(); we don't add a second denial.
        return False

    # Protected top-level dirs
    first = _first_segment(rel)
    if first is None:
        # Path == WORKSPACE_ROOT itself — never meaningful as a write target
        # for file tools, treat as protected.
        return True

    if first in _SELF_SOURCE_DIRS or first in _SELF_AUX_DIRS:
        return True

    # Protected root files
    if rel.name in _SELF_ROOT_FILES:
        return True

    return False


def deny_reason(path: str, workspace_root: Path) -> str:
    """Human-readable reason for the denial — included in the ToolResult.error.

    The message is worded for the LLM (who will surface it back to the user
    in most cases) and tells them the exact opt-out to set if they really
    want to proceed.
    """
    return (
        f"Refused to write '{path}': this path lives inside the agent's own "
        f"source tree (workspace={workspace_root}).\n"
        f"Manual edits to the agent are protected from accidental overwrite "
        f"by the agent's own write tools.\n"
        f"If you genuinely need to modify the agent, ask the user to run with "
        f"CODING_AGENT_ALLOW_SELF_MODIFY=1."
    )


def reset_for_test() -> None:
    """Test hook — clear the env-var override. Most tests don't need this."""
    os.environ.pop("CODING_AGENT_ALLOW_SELF_MODIFY", None)


__all__ = [
    "is_self_source_workspace",
    "is_protected_path",
    "deny_reason",
    "reset_for_test",
]
