"""Task state machine — drives long-running task progression (PR-03).

States: INIT → PLAN → EXEC → TEST → REVIEW → DONE
        └────── FAILED (recoverable to INIT/PLAN) ──────┘

Why a state machine for tasks (not just TDD)? Long tasks (50+ steps) easily
"drift" — the LLM forgets where it is, what was completed, what failed.
Persisting the state to `~/.coding-agent/task_state.json` enables:
  - Process-crash recovery (`coding-agent --resume`)
  - Auditability (what phase was active at any moment)
  - State-based injection ("You are in EXEC; current step is X")

This complements (not replaces) the existing TDD state machine (PR-02), which
operates *within* a single phase. Task state machine operates at task-grain.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskState(Enum):
    """High-level task lifecycle states."""

    INIT = "init"
    PLAN = "plan"
    EXEC = "exec"
    TEST = "test"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


# Legal transitions. Each maps current state → set of allowed next states.
# FAILED is a sink that can recover to INIT or PLAN.
_ALLOWED_TRANSITIONS = {
    TaskState.INIT: {TaskState.PLAN, TaskState.FAILED},
    TaskState.PLAN: {TaskState.EXEC, TaskState.INIT, TaskState.FAILED},
    TaskState.EXEC: {TaskState.TEST, TaskState.PLAN, TaskState.FAILED},
    TaskState.TEST: {TaskState.REVIEW, TaskState.EXEC, TaskState.FAILED},
    TaskState.REVIEW: {TaskState.DONE, TaskState.EXEC, TaskState.FAILED},
    TaskState.DONE: set(),  # terminal
    TaskState.FAILED: {TaskState.INIT, TaskState.PLAN},
}


class InvalidStateTransition(Exception):
    """Raised when an illegal task state transition is attempted."""

    pass


@dataclass
class TaskStateRecord:
    """Persisted snapshot of a single task's progress.

    Serialized to JSON; atomic write ensures crash safety.
    """

    task: str
    state: str  # TaskState.value
    created_at: str
    updated_at: str
    session_id: str = ""
    completed_steps: list = field(default_factory=list)  # list of dicts
    current_step: Optional[dict] = None
    next_step: Optional[dict] = None
    known_issues: list = field(default_factory=list)  # list of strings
    op_hash: str = ""  # sha256 of last op, for tamper detection
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskStateRecord":
        # Tolerate older schemas by ignoring unknown fields
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class TaskStateMachine:
    """Single source of truth for task progress.

    Backed by a JSON file; atomic writes via tmp + replace.
    """

    DEFAULT_STATE_FILE = Path.home() / ".coding-agent" / "task_state.json"

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or self.DEFAULT_STATE_FILE
        self.record: TaskStateRecord = self._load_or_init()

    def _load_or_init(self) -> TaskStateRecord:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                rec = TaskStateRecord.from_dict(data)
                return rec
            except (json.JSONDecodeError, OSError, KeyError):
                # Corrupt file — start fresh, but don't lose the old one
                backup = self.state_file.with_suffix(f".corrupt.{int(time.time())}.json")
                try:
                    self.state_file.rename(backup)
                except OSError:
                    pass
        return TaskStateRecord(
            task="",
            state=TaskState.INIT.value,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

    def _save(self) -> None:
        """Atomic write: tmp file + replace."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.record.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            # If atomic fails, fall back to direct write
            self.state_file.write_text(
                json.dumps(self.record.to_dict(), indent=2), encoding="utf-8"
            )

    def transition(self, new_state: TaskState, **kwargs) -> None:
        """Move to new_state. Raises InvalidStateTransition if illegal.

        Side effects: updates updated_at, optionally updates other fields via kwargs.
        """
        current = TaskState(self.record.state)
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if new_state not in allowed:
            allowed_names = [s.value for s in allowed]
            raise InvalidStateTransition(
                f"Cannot transition {current.value} → {new_state.value}. "
                f"Allowed: {allowed_names}"
            )
        self.record.state = new_state.value
        self.record.updated_at = datetime.now().isoformat()
        for k, v in kwargs.items():
            if hasattr(self.record, k):
                setattr(self.record, k, v)
        self._save()

    def record_completed_step(self, tool: str, args: dict, result_hash: str) -> None:
        """Append a completed step. Updates op_hash chain."""
        step = {
            "tool": tool,
            "args_summary": self._summarize_args(args),
            "result_hash": result_hash,
            "ts": datetime.now().isoformat(),
        }
        self.record.completed_steps.append(step)
        # Update chain hash
        prev = self.record.op_hash
        self.record.op_hash = self._chain_hash(prev, f"{tool}:{result_hash}")
        self.record.updated_at = datetime.now().isoformat()
        self._save()

    def add_known_issue(self, issue: str) -> None:
        if issue not in self.record.known_issues:
            self.record.known_issues.append(issue)
            self.record.updated_at = datetime.now().isoformat()
            self._save()

    def start_task(self, task: str, session_id: str = "") -> None:
        """Initialize a new task. Resets completed steps."""
        self.record = TaskStateRecord(
            task=task,
            state=TaskState.INIT.value,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            session_id=session_id,
        )
        self._save()

    @staticmethod
    def _summarize_args(args: dict) -> str:
        """Truncate args for compact storage."""
        s = json.dumps(args, sort_keys=True, default=str)
        return s[:200] + ("…" if len(s) > 200 else "")

    @staticmethod
    def _chain_hash(prev: str, op: str) -> str:
        """sha256 of (prev + op) for tamper-evident chain."""
        h = hashlib.sha256(f"{prev}{op}".encode("utf-8")).hexdigest()[:32]
        return f"sha256:{h}"

    @property
    def state(self) -> TaskState:
        return TaskState(self.record.state)

    def summary(self) -> dict:
        """Snapshot for /status and system-reminder injection."""
        return {
            "state": self.record.state,
            "task": self.record.task,
            "completed_steps": len(self.record.completed_steps),
            "current_step": self.record.current_step,
            "next_step": self.record.next_step,
            "known_issues": list(self.record.known_issues),
            "updated_at": self.record.updated_at,
            "session_id": self.record.session_id,
        }

    def format_reminder(self) -> str:
        """Format as system-reminder string for injection into user message."""
        if not self.record.task:
            return ""
        return (
            f"[Task State: {self.record.state}]\n"
            f"Task: {self.record.task}\n"
            f"Completed: {len(self.record.completed_steps)} steps\n"
            f"Current: {self._fmt_step(self.record.current_step)}\n"
            f"Next: {self._fmt_step(self.record.next_step)}\n"
            f"Known issues: {', '.join(self.record.known_issues) or 'none'}"
        )

    @staticmethod
    def _fmt_step(step) -> str:
        if not step:
            return "—"
        if isinstance(step, dict):
            return step.get("description", str(step))
        return str(step)

    def delete(self) -> None:
        """Remove the state file. Useful for starting fresh."""
        if self.state_file.exists():
            self.state_file.unlink()
        self.record = TaskStateRecord(
            task="",
            state=TaskState.INIT.value,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
