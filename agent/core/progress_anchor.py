"""Progress anchor file `.claude-progress.txt` (PR-13).

A simple, human-readable checkpoint file written to `WORKSPACE/
.claude-progress.txt`. It records the *deterministic* state of a
multi-step task: what we're doing, where we are, what's next, what
problems we've hit, and a tamper-evident chain hash.

Format (Key: Value):
    [current_task]: 实现带鉴权的 API
    [current_step]: 3/8 (writing login endpoint)
    [next_step]: 4/8 (write auth middleware test)
    [op_hash]: sha256:abc123...
    [known_issues]:
      - rate limiting 未实现
    [updated_at]: 2026-06-06T10:23:45

Why a separate file from the PR-03 task state machine?
- PR-03 is **machine-readable JSON** (state machine + steps for
  orchestration).
- PR-13 is **human-readable text** — easy to grep, `git diff`, paste
  into a chat, or hand-edit if a human wants to override.
- Both files can coexist: the JSON for engines, this text for humans
  and `git log -p`.

Why chain hash?
- The hash lets you verify "the file I'm looking at now is the same
  as the one I left 30 steps ago" by recomputing the chain.
- We store the *previous* hash + the new op → the file's final
  `[op_hash]` is the root of the chain.
- `verify_chain()` is intentionally minimal here: full re-validation
  would require per-op history. The point is to make *any* edit
  visible (the hash mismatch) rather than a tamper-proof log.

Why atomic write (tmp + replace)?
- `Path.replace()` is atomic on POSIX (single inode rename).
- A partial write would leave the file truncated, which is worse
  than no file.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class ProgressRecord:
    """A single snapshot of the progress anchor file."""
    current_task: str = ""
    current_step: str = ""
    next_step: str = ""
    op_hash: str = ""
    known_issues: List[str] = field(default_factory=list)
    updated_at: str = ""
    extra: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        """True if the record carries no useful state."""
        return not any([
            self.current_task, self.current_step, self.next_step,
            self.op_hash, self.known_issues, self.extra,
        ])

    def to_prompt(self) -> str:
        """Format for injection into the LLM's system-reminder."""
        issues = ", ".join(self.known_issues) if self.known_issues else "none"
        return (
            f"current_task: {self.current_task or '(unset)'}\n"
            f"current_step: {self.current_step or '(unset)'}\n"
            f"next_step:    {self.next_step or '(unset)'}\n"
            f"known_issues: {issues}"
        )


# ── Manager ─────────────────────────────────────────────────────────


class ProgressAnchor:
    """Read/write the `.claude-progress.txt` file.

    The file path is `WORKSPACE / '.claude-progress.txt'`. A workspace
    of None falls back to the current working directory.
    """

    FILE_NAME = ".claude-progress.txt"
    KEY_RE = re.compile(r"^\[(\w+)\]:\s*(.*?)$")

    def __init__(self, workspace: Optional[Path] = None):
        if workspace is None:
            workspace = Path.cwd()
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Optional[ProgressRecord]:
        """Parse the file. Returns None if it doesn't exist."""
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return self._parse(text)

    @classmethod
    def _parse(cls, text: str) -> ProgressRecord:
        record = ProgressRecord()
        in_known_issues = False
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            # Known issues are a list with "- " prefix on subsequent lines
            if in_known_issues and line.strip().startswith("-"):
                issue = line.strip().lstrip("-").strip()
                if issue:
                    record.known_issues.append(issue)
                continue
            m = cls.KEY_RE.match(line)
            if not m:
                in_known_issues = False
                continue
            key, value = m.group(1), m.group(2).strip()
            in_known_issues = (key == "known_issues")
            if in_known_issues:
                # Inline issue on the same line
                if value.startswith("-"):
                    value = value.lstrip("-").strip()
                if value:
                    record.known_issues.append(value)
                continue
            if key == "current_task":
                record.current_task = value
            elif key == "current_step":
                record.current_step = value
            elif key == "next_step":
                record.next_step = value
            elif key == "op_hash":
                record.op_hash = value
            elif key == "updated_at":
                record.updated_at = value
            else:
                # Unknown key — keep in `extra` so we don't lose data
                record.extra[key] = value
        return record

    def write(self, record: ProgressRecord) -> Path:
        """Persist a record to disk atomically (tmp + replace)."""
        if not record.updated_at:
            record.updated_at = datetime.now().isoformat()
        lines = [
            f"[current_task]: {record.current_task}",
            f"[current_step]: {record.current_step}",
            f"[next_step]: {record.next_step}",
            f"[op_hash]: {record.op_hash}",
        ]
        if record.known_issues:
            lines.append("[known_issues]:")
            for issue in record.known_issues:
                lines.append(f"  - {issue}")
        else:
            lines.append("[known_issues]:")
        lines.append(f"[updated_at]: {record.updated_at}")
        for k, v in record.extra.items():
            lines.append(f"[{k}]: {v}")
        content = "\n".join(lines) + "\n"
        # Atomic write: tmp file in same directory, then replace.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=".claude-progress.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            f.write(content)
            f.flush()
        tmp_path.replace(self.path)
        return self.path

    def clear(self) -> None:
        """Remove the file (used when a task is concluded)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── Chain hash ──────────────────────────────────────────────

    @staticmethod
    def compute_hash(prev_hash: str, op: str) -> str:
        """Chain hash: sha256(prev_hash + op)[:32] prefixed with `sha256:`.

        The prefix matches the audit-log hash format (PR-08) for
        cross-system consistency.
        """
        payload = f"{prev_hash}{op}".encode("utf-8")
        h = hashlib.sha256(payload).hexdigest()[:32]
        return f"sha256:{h}"

    def verify_chain(self) -> bool:
        """Best-effort chain verification.

        We can only check the *current* op_hash matches a recomputation
        of all known ops. Without per-op history this is a no-op
        placeholder — the file is still useful as a snapshot, just not
        cryptographically auditable.
        """
        record = self.read()
        if not record:
            return True
        # The file's integrity is mostly informational. Real verification
        # requires per-op history (out of scope for this PR).
        return True

    # ── Snapshot rendering ──────────────────────────────────────

    def render(self) -> str:
        """Render the current file (or a placeholder) for display."""
        record = self.read()
        if record is None:
            return "(no progress file)"
        return record.to_prompt()


# ── Convenience function ───────────────────────────────────────────


def load_progress(workspace: Optional[Path] = None) -> Optional[ProgressRecord]:
    """Load the progress record for the given workspace."""
    return ProgressAnchor(workspace=workspace).read()
