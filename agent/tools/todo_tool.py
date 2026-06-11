"""TodoWrite tool — track multi-step task progress."""

import json

from .base import BaseTool, ToolResult, registry


class TodoWriteTool(BaseTool):
    """Create and update a structured task list for tracking progress."""

    user_facing_name = "Todo"
    is_concurrency_safe = False
    is_read_only = False

    name = "todo_write"
    description = (
        "Create and update a structured task list. Use this to track progress "
        "on multi-step tasks. Each task has: id, status (pending/in_progress/completed), "
        "content (description). Mark tasks in_progress BEFORE starting them, "
        "and completed as soon as done."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "string",
                            "description": (
                                "JSON array of todo items. Each item: "
                                '{"id": "1", "status": "pending|in_progress|completed", "content": "Fix login bug"}. '
                                "Always include ALL tasks, not just the changed ones."
                            ),
                        },
                    },
                    "required": ["todos"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        try:
            todos = json.loads(args.get("todos", "[]"))
            counts = {"completed": 0, "in_progress": 0, "pending": 0}
            for t in todos:
                s = t.get("status", "pending")
                if s in counts:
                    counts[s] += 1
            parts = []
            if counts["completed"]:
                parts.append(f"{counts['completed']} done")
            if counts["in_progress"]:
                parts.append(f"{counts['in_progress']} doing")
            if counts["pending"]:
                parts.append(f"{counts['pending']} todo")
            return f"Todo · {', '.join(parts)}" if parts else "Todo · empty"
        except Exception:
            return "Todo"

    def render_result(self, result: ToolResult) -> str:
        if result.success and result.metadata:
            total = result.metadata.get("total", 0)
            done = result.metadata.get("done", 0)
            return f"Todo · {done}/{total} done"
        return super().render_result(result)

    async def execute(self, todos: str, **kwargs) -> ToolResult:
        try:
            items = json.loads(todos)
            if not isinstance(items, list):
                return ToolResult(success=False, content="", error="todos must be a JSON array")

            total = len(items)
            done = sum(1 for t in items if t.get("status") == "completed")
            in_progress = sum(1 for t in items if t.get("status") == "in_progress")
            pending = total - done - in_progress

            # Format display
            lines = []
            for t in items:
                tid = t.get("id", "?")
                status = t.get("status", "pending")
                content = t.get("content", "")[:80]
                icons = {"completed": "✓", "in_progress": "●", "pending": "○"}
                icon = icons.get(status, "?")
                lines.append(f"  {icon} [{tid}] {content}")

            return ToolResult(
                success=True,
                content="\n".join(lines) if lines else "(empty)",
                metadata={"total": total, "done": done, "pending": pending, "tasks": items},
            )
        except json.JSONDecodeError as e:
            return ToolResult(success=False, content="", error=f"Invalid JSON: {e}")


registry.register(TodoWriteTool())
