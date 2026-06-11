"""Error recovery — classify tool failures and suggest corrective actions.

The engine calls recover() after a tool fails. If a recovery strategy is found,
the corrected args are returned and the engine retries automatically.
"""

from typing import Optional


def recover(tool_name: str, args: dict, error: str) -> Optional[dict]:
    """Try to recover from a tool failure. Returns corrected args or None.

    Called by the engine after _execute_tool() returns a failure.
    If recovery succeeds, the engine retries with corrected args.
    """
    if tool_name == "read_file":
        return _recover_read_file(args, error)
    if tool_name == "install_package":
        return _recover_install(args, error)
    if tool_name == "execute_command":
        return _recover_shell(args, error)
    return None


def _recover_read_file(args: dict, error: str) -> Optional[dict]:
    """File not found → try fuzzy name matching in same directory."""
    if "not found" not in error.lower():
        return None
    import os
    from pathlib import Path
    path = Path(args.get("path", ""))
    if not path.parent.exists():
        return None
    # List sibling files, find closest match
    siblings = list(path.parent.iterdir())
    if not siblings:
        return None
    name = path.name.lower()
    best = None
    best_dist = float("inf")
    for sib in siblings:
        dist = _levenshtein(name, sib.name.lower())
        if dist < best_dist and dist < 5:
            best_dist = dist
            best = sib
    if best:
        return {**args, "path": str(best)}
    return None


def _recover_install(args: dict, error: str) -> Optional[dict]:
    """Package not found → try common suffixes (already handled in install.py).
    This is a fallback for execute_command-based pip failures."""
    if "pip" not in error.lower() and "not found" not in error.lower():
        return None
    pkg = args.get("package", "")
    if not pkg or "-" in pkg:
        return None
    for suffix in ["-agent", "-cli", "-tool", "-sdk", "-python", "-js"]:
        pkg_name = pkg + suffix
        return {**args, "package": pkg_name}
    return None


def _recover_shell(args: dict, error: str) -> Optional[dict]:
    """Shell command timeout → retry once with longer timeout."""
    if "timeout" not in error.lower() and "No output" not in error:
        return None
    timeout = args.get("timeout", 30)
    if timeout >= 120:
        return None  # Already tried
    return {**args, "timeout": min(timeout * 2, 120)}


def _levenshtein(a: str, b: str) -> int:
    """Simple Levenshtein distance."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,      # deletion
                curr[j] + 1,           # insertion
                prev[j] + (ca != cb),  # substitution
            ))
        prev = curr
    return prev[-1]
