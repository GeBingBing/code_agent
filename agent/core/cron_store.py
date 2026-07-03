"""Scheduled-task store for cron-style prompts.

Persists scheduled tasks to ``~/.coding-agent/scheduled_tasks.json`` so they
survive across sessions. The CLI checks for due tasks when the REPL is idle
and injects the task's prompt as a new user turn — matching Claude Code's
CronCreate behavior (jobs only fire while the REPL is idle).

A task is either recurring (``recurring=True``, standard 5-field cron) or
one-shot (``recurring=False``, fires once at the next matching minute then
auto-deletes).
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


def _store_path() -> Path:
    d = Path(os.path.expanduser("~/.coding-agent"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "scheduled_tasks.json"


@dataclass
class ScheduledTask:
    id: str
    cron: str  # 5-field cron (min hour dom month dow), local time
    prompt: str  # the prompt to enqueue when the task fires
    recurring: bool  # True = repeat, False = fire once then delete
    durable: bool  # True = persist to disk; False = session-only (in-memory)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledTask":
        return cls(
            id=d["id"],
            cron=d["cron"],
            prompt=d["prompt"],
            recurring=d.get("recurring", True),
            durable=d.get("durable", True),
        )


class CronStore:
    """In-memory + on-disk store of scheduled tasks."""

    def __init__(self) -> None:
        self._tasks: List[ScheduledTask] = []
        self._load()

    def _load(self) -> None:
        p = _store_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._tasks = [ScheduledTask.from_dict(t) for t in data]
        except (json.JSONDecodeError, KeyError, TypeError):
            # Corrupt file — start fresh rather than crash.
            self._tasks = []

    def _save(self) -> None:
        durable = [t for t in self._tasks if t.durable]
        p = _store_path()
        p.write_text(
            json.dumps([t.to_dict() for t in durable], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> List[ScheduledTask]:
        return list(self._tasks)

    def add(self, task: ScheduledTask) -> None:
        self._tasks.append(task)
        if task.durable:
            self._save()

    def remove(self, task_id: str) -> bool:
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.id != task_id]
        removed = len(self._tasks) < before
        if removed:
            self._save()
        return removed

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None


# Module-level singleton — the CLI and the cron_create/cron_delete tools
# share one store so a created task is immediately visible to the idle loop.
_store: Optional[CronStore] = None


def get_cron_store() -> CronStore:
    global _store
    if _store is None:
        _store = CronStore()
    return _store
