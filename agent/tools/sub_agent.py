"""Sub-agent tool - spawn child agents for task decomposition.

支持：
- 树形嵌套subagent
- 深度限制（通过SubAgentRegistry）
- 生命周期管理（spawn/complete/kill）
- 模型选择
- 子agent列表和杀死
"""

import asyncio
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry
from ..core.subagent_registry import get_registry, SubAgentStatus


class SpawnSubAgentTool(BaseTool):
    user_facing_name = "Agent"

    name = "spawn_sub_agent"
    description = (
        "Spawn a sub-agent to handle a subtask. Set background=true to run "
        "asynchronously and continue the conversation while it works."
    )

    def get_activity_description(self, args: dict) -> str:
        """Spinner activity verb — include the sub-agent's label so the
        user can see WHICH sub-agent is running (e.g. "子 Agent explorer...").
        """
        label = args.get("label") or "subagent"
        return f"子 Agent {label}..."

    async def execute(
        self,
        task: str,
        label: Optional[str] = None,
        model: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        background: bool = False,
        **kwargs,
    ) -> ToolResult:
        """Spawn a sub-agent to handle a subtask.

        Args:
            task: The subtask for the child agent to handle
            label: Optional label for this sub-agent (for tracking)
            model: Optional model override for the sub-agent
            parent_run_id: Parent agent's run_id for tree tracking
            background: If True, return task_id immediately and run in background
        """
        try:
            from ..core.engine import AgentEngine, AgentConfig
            from ..core.subagent_registry import get_registry

            reg = get_registry()

            depth = 0
            if parent_run_id:
                parent = reg.get(parent_run_id)
                if parent:
                    depth = parent.depth + 1

            record = reg.spawn(
                parent_id=parent_run_id,
                label=label or "subagent",
                task=task,
                depth=depth,
            )

            run_id = record.id
            config = AgentConfig(model=model) if model else AgentConfig()
            config.verbose = False
            sub_agent = AgentEngine(config)

            async def run_and_complete():
                try:
                    result = await sub_agent.run(task)
                    reg.complete(run_id, result)
                    return result
                except asyncio.CancelledError:
                    reg.fail(run_id, "Task cancelled")
                    raise
                except Exception as e:
                    reg.fail(run_id, str(e))
                    return f"Error: {e}"

            async_task = asyncio.create_task(run_and_complete())
            reg.register_task(run_id, async_task)

            if background:
                # Return immediately — task runs in background
                reg.set_background(run_id, True)
                return ToolResult(
                    success=True,
                    content=f"Background task started: {run_id} — {label or task[:40]}\n"
                            f"Use list_sub_agents to check status.",
                    metadata={"task_id": run_id, "background": True},
                )

            # Synchronous wait
            try:
                result = await async_task
                if isinstance(result, str) and result.startswith("Error:"):
                    return ToolResult(
                        success=False, content=result, error=result,
                        metadata={"task_id": run_id},
                    )
                return ToolResult(
                    success=True,
                    content=f"[Sub-agent {label or run_id} result]\n{result}",
                    metadata={"task_id": run_id},
                )
            except Exception as e:
                return ToolResult(success=False, content="", error=str(e))

        except ValueError as e:
            return ToolResult(success=False, content="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "task": {
                            "type": "string",
                            "description": "The subtask for the child agent to handle",
                        },
                        "label": {
                            "type": "string",
                            "description": "Optional label for this sub-agent",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model override for the sub-agent",
                        },
                        "parent_run_id": {
                            "type": "string",
                            "description": "Parent agent's run_id for tree tracking",
                        },
                        "background": {
                            "type": "boolean",
                            "description": "If true, return task_id immediately; sub-agent runs in background",
                            "default": False,
                        },
                    },
                    "required": ["task"],
                },
            },
        }


class ListSubAgentsTool(BaseTool):
    user_facing_name = "List"

    is_concurrency_safe = True
    is_read_only = True
    name = "list_sub_agents"
    description = "List all sub-agents or show children of a specific sub-agent"

    async def execute(self, parent_id: Optional[str] = None, show_tree: bool = False, **kwargs) -> ToolResult:
        """List sub-agents.

        Args:
            parent_id: If provided, show children of this sub-agent. Otherwise show all.
            show_tree: If True, show full tree structure
        """
        try:
            from ..core.subagent_registry import get_registry

            registry = get_registry()

            if show_tree and parent_id:
                tree = registry.get_tree(parent_id)
                import json
                return ToolResult(
                    success=True,
                    content=f"Sub-agent tree:\n{json.dumps(tree, indent=2)}",
                )
            elif parent_id:
                children = registry.list_children(parent_id)
                if not children:
                    return ToolResult(success=True, content="No children")
                lines = [f"Children of {parent_id}:"]
                for c in children:
                    lines.append(f"  [{c.status.value}] {c.label or c.id} (depth={c.depth})")
                return ToolResult(success=True, content="\n".join(lines))
            else:
                active = registry.list_active()
                all_records = registry.list_all()

                lines = [f"Total sub-agents: {len(all_records)}"]
                lines.append(f"Active: {len(active)}")

                if active:
                    lines.append("\nActive sub-agents:")
                    for r in active:
                        lines.append(f"  [{r.status.value}] {r.label or r.id} depth={r.depth}")

                return ToolResult(success=True, content="\n".join(lines))

        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "parent_id": {
                            "type": "string",
                            "description": "Parent sub-agent ID",
                        },
                        "show_tree": {
                            "type": "boolean",
                            "description": "Show full tree structure",
                        },
                    },
                },
            },
        }


class KillSubAgentTool(BaseTool):
    user_facing_name = "Kill"

    name = "kill_sub_agent"
    description = "Kill a running sub-agent and all its children"

    async def execute(self, run_id: str, **kwargs) -> ToolResult:
        """Kill a sub-agent.

        Args:
            run_id: The sub-agent run_id to kill
        """
        try:
            from ..core.subagent_registry import get_registry

            registry = get_registry()
            success = registry.kill(run_id)

            if success:
                return ToolResult(success=True, content=f"Killed sub-agent {run_id}")
            else:
                return ToolResult(success=False, content="", error=f"Sub-agent {run_id} not found")

        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "run_id": {
                            "type": "string",
                            "description": "The sub-agent run_id to kill",
                        },
                    },
                    "required": ["run_id"],
                },
            },
        }


class SpawnParallelTool(BaseTool):
    user_facing_name = "Parallel"

    name = "spawn_parallel"
    description = "Spawn multiple sub-agents to handle independent subtasks concurrently and return merged results"

    async def execute(
        self,
        tasks: str,
        parent_run_id: Optional[str] = None,
        max_parallel: int = 5,
        **kwargs,
    ) -> ToolResult:
        """Spawn multiple sub-agents in parallel.

        Args:
            tasks: JSON array of {task, label} objects, e.g.
                   '[{"task": "write auth.py", "label": "auth"}, {"task": "write db.py", "label": "db"}]'
            parent_run_id: Parent agent's run_id for tree tracking
            max_parallel: Max concurrent sub-agents (default 5)
        """
        import json as _json

        try:
            task_list = _json.loads(tasks)
            if not isinstance(task_list, list):
                return ToolResult(success=False, content="", error="tasks must be a JSON array")
        except _json.JSONDecodeError as e:
            return ToolResult(success=False, content="", error=f"Invalid JSON: {e}")

        if not task_list:
            return ToolResult(success=False, content="", error="Empty task list")

        from ..core.engine import AgentEngine, AgentConfig
        from ..core.subagent_registry import get_registry

        reg = get_registry()

        async def _run_one(subtask: dict, idx: int) -> dict:
            """Run a single sub-agent and return structured result."""
            task_desc = subtask.get("task", "")
            label = subtask.get("label", f"parallel-{idx}")
            model = subtask.get("model", None)

            # Get depth from parent
            depth = 0
            if parent_run_id:
                parent = reg.get(parent_run_id)
                if parent:
                    depth = parent.depth + 1

            try:
                record = reg.spawn(parent_id=parent_run_id, label=label, task=task_desc, depth=depth)
            except ValueError as e:
                return {"label": label, "success": False, "error": str(e)}

            run_id = record.id

            try:
                config = AgentConfig(model=model) if model else AgentConfig()
                config.verbose = False
                sub_agent = AgentEngine(config)
                result = await sub_agent.run(task_desc)
                reg.complete(run_id, result)
                # Detect error string
                if isinstance(result, str) and result.startswith("Error:"):
                    return {"label": label, "success": False, "content": result}
                return {"label": label, "success": True, "content": str(result)}
            except asyncio.CancelledError:
                reg.fail(run_id, "Cancelled")
                return {"label": label, "success": False, "error": "Cancelled"}
            except Exception as e:
                reg.fail(run_id, str(e))
                return {"label": label, "success": False, "error": str(e)}

        # Run in parallel with semaphore to limit concurrency
        sem = asyncio.Semaphore(max_parallel)

        async def _run_with_semaphore(subtask, idx):
            async with sem:
                return await _run_one(subtask, idx)

        coros = [_run_with_semaphore(task_list[i], i) for i in range(len(task_list))]
        results = await asyncio.gather(*coros)

        # Merge results
        ok = sum(1 for r in results if r.get("success"))
        fail = len(results) - ok
        lines = [f"Parallel execution: {ok}/{len(results)} succeeded, {fail} failed", ""]
        for r in results:
            status = "✓" if r.get("success") else "✗"
            content = r.get("content") or r.get("error", "")
            lines.append(f"  [{status}] {r['label']}: {content[:120]}")
        return ToolResult(success=fail == 0, content="\n".join(lines))

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
                        "tasks": {
                            "type": "string",
                            "description": "JSON array of {task, label} objects for parallel execution",
                        },
                        "parent_run_id": {
                            "type": "string",
                            "description": "Parent agent's run_id for tree tracking",
                        },
                        "max_parallel": {
                            "type": "integer",
                            "description": "Max concurrent sub-agents (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["tasks"],
                },
            },
        }


# Register tools
registry.register(SpawnSubAgentTool())
registry.register(SpawnParallelTool())
registry.register(ListSubAgentsTool())
registry.register(KillSubAgentTool())