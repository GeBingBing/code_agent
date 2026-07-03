"""TodoStore — task list that survives across turns, injected into every reminder.

TodoWriteTool writes to this store; the engine injects the current list into
every turn's <system-reminder> so the LLM always sees its task state (Claude
Code does the same — without it, the LLM reconstructs the list from chat
history and frequently loses track).

Module-level singleton (like cron_store): the tool and the engine share one
instance without the tool needing an engine reference.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TodoItem:
    id: str
    content: str
    status: str = "pending"  # pending | in_progress | completed


@dataclass
class TodoStore:
    """In-memory todo list for the current task."""

    items: List[TodoItem] = field(default_factory=list)

    def replace_all(self, raw_items: List[dict]) -> List[TodoItem]:
        """Replace the whole list (TodoWriteTool sends the full list each call).

        Enforces at most one in_progress (Claude Code convention): keeps the
        first, demotes the rest to pending.
        """
        seen_ip = False
        new: List[TodoItem] = []
        for t in raw_items:
            status = t.get("status", "pending")
            if status == "in_progress":
                if seen_ip:
                    status = "pending"
                else:
                    seen_ip = True
            new.append(
                TodoItem(
                    id=str(t.get("id", "?")),
                    content=str(t.get("content", ""))[:120],
                    status=status,
                )
            )
        self.items = new
        return new

    def clear(self) -> None:
        self.items = []

    def to_prompt(self) -> str:
        """Render the list for <system-reminder> injection."""
        if not self.items:
            return ""
        icons = {"completed": "✓", "in_progress": "●", "pending": "○"}
        lines = []
        for t in self.items:
            icon = icons.get(t.status, "?")
            lines.append(f"  {icon} [{t.id}] {t.content}")
        done = sum(1 for t in self.items if t.status == "completed")
        header = f"Tasks ({done}/{len(self.items)} done):"
        return header + "\n" + "\n".join(lines)


# Module-level singleton — the tool and the engine share one store.
_store: Optional[TodoStore] = None


def get_todo_store() -> TodoStore:
    global _store
    if _store is None:
        _store = TodoStore()
    return _store


def reset_todo_store() -> None:
    """Clear the singleton (used by /clear and tests)."""
    global _store
    _store = None
