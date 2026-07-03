"""Cron tools — schedule prompts to fire on a cron schedule (Claude Code CronCreate style).

Two tools:
  - cron_create: schedule a prompt to fire on a 5-field cron (local time).
    recurring=True repeats; recurring=False fires once then auto-deletes.
  - cron_delete: cancel a scheduled task by id.

Jobs only fire while the REPL is idle — the CLI's idle loop checks the store
each tick and enqueues any due prompt as a new user turn. Matches Claude Code.
"""

import uuid

from ..core.cron_store import ScheduledTask, get_cron_store
from .base import BaseTool, ToolResult, registry


class CronCreateTool(BaseTool):
    user_facing_name = "Cron"

    is_concurrency_safe = True
    is_read_only = True  # schedules a future prompt; doesn't modify the repo
    name = "cron_create"
    description = (
        "Schedule a prompt to fire on a recurring schedule (cron expression). "
        "The prompt is enqueued as a new user turn when the REPL is idle. "
        "Use for recurring checks ('every weekday at 9am, review open PRs') or "
        "one-shot reminders ('in 2 hours, check the deploy'). Cron is 5-field "
        "local time: minute hour day-of-month month day-of-week. recurring=true "
        "repeats; recurring=false fires once then auto-deletes."
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
                        "cron": {
                            "type": "string",
                            "description": (
                                "5-field cron in local time, e.g. '0 9 * * 1-5' "
                                "(weekdays 9am), '*/5 * * * *' (every 5 min)."
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The prompt to enqueue when the task fires.",
                        },
                        "recurring": {
                            "type": "boolean",
                            "description": "True = repeat on schedule; False = fire once then delete.",
                            "default": True,
                        },
                        "durable": {
                            "type": "boolean",
                            "description": "True (default) = persist to disk, survives restart. False = session-only.",
                            "default": True,
                        },
                    },
                    "required": ["cron", "prompt"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        return f"{args.get('cron', '?')} — {args.get('prompt', '')[:40]}"

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.content:
            return result.content
        return super().render_result(result)

    async def execute(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
        **kwargs,
    ) -> ToolResult:
        # Basic cron validation: 5 whitespace-separated fields.
        fields = cron.split()
        if len(fields) != 5:
            return ToolResult(
                success=False,
                content="",
                error=f"cron must have 5 fields (min hour dom month dow), got {len(fields)}",
            )
        task = ScheduledTask(
            id=uuid.uuid4().hex[:8],
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
        )
        get_cron_store().add(task)
        kind = "recurring" if recurring else "one-shot"
        return ToolResult(
            success=True,
            content=f"Scheduled {kind} task {task.id}: '{cron}' → {prompt[:60]}",
            metadata={"task_id": task.id},
        )


class CronDeleteTool(BaseTool):
    user_facing_name = "Cron"

    is_concurrency_safe = True
    is_read_only = True
    name = "cron_delete"
    description = "Cancel a scheduled task by its id."

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
                        "task_id": {
                            "type": "string",
                            "description": "The id returned by cron_create.",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        return args.get("task_id", "?")

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.content:
            return result.content
        return super().render_result(result)

    async def execute(self, task_id: str, **kwargs) -> ToolResult:
        removed = get_cron_store().remove(task_id)
        if not removed:
            return ToolResult(success=False, content="", error=f"No task with id {task_id}")
        return ToolResult(success=True, content=f"Cancelled task {task_id}")


registry.register(CronCreateTool())
registry.register(CronDeleteTool())
