"""Error recovery — classify tool failures and suggest corrective actions.

The engine calls recover() after a tool fails. If a recovery strategy is found,
the corrected args are returned and the engine retries automatically.
"""

from typing import Optional

# Commands that stay in the foreground serving requests. When such a command
# is killed by the idle/wall-clock timeout, the right fix is to background it
# (execute_command(background=True)) — NOT to retry it in the foreground with a
# bigger timeout, which just delays the same death. Matching is substring-based
# on the command string (case-insensitive).
LONG_RUNNING_PATTERNS = (
    "npm run dev",
    "npm start",
    "npm run serve",
    "next dev",
    "next start",
    "npx serve",
    "npx next",
    "vite",
    "uvicorn",
    "gunicorn",
    "flask run",
    "rails s",
    "rails server",
    "python -m http.server",
    "http-server",
    "serve ",
    "live-server",
    "webpack serve",
)


def _is_long_running_server(command: str) -> bool:
    cmd_lower = (command or "").lower()
    return any(pat in cmd_lower for pat in LONG_RUNNING_PATTERNS)


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
    """Recover from a shell failure.

    Two strategies, chosen by reading the error:

    1. Long-running server killed by timeout → switch to background mode.
       A dev server (npm run dev, npx serve, uvicorn, ...) prints a "ready"
       line then goes silent listening on a socket — the idle timeout kills
       it. Retrying in the foreground with a bigger timeout just delays the
       same death. So re-issue the SAME command with background=True (output
       to a log file, detached), which is the correct way to run a server.

    2. One-shot command that timed out → retry once with a longer timeout
       (capped at 120s). The command may legitimately need more time (slow
       build, big test suite) but isn't a server.
    """
    err_lower = error.lower()
    is_timeout = "timeout" in err_lower or "no output" in err_lower
    if not is_timeout:
        return None

    command = args.get("command", "")
    already_background = args.get("background", False)

    # Strategy 1: server → background it.
    if _is_long_running_server(command) and not already_background:
        return {**args, "background": True, "timeout": 30, "max_wait_seconds": 0}

    # Strategy 2: one-shot → bigger timeout (one retry).
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
            curr.append(
                min(
                    prev[j + 1] + 1,  # deletion
                    curr[j] + 1,  # insertion
                    prev[j] + (ca != cb),  # substitution
                )
            )
        prev = curr
    return prev[-1]
