"""PlanWorkflow — plan-then-execute orchestration (PR-21, extracted from AgentEngine).

The plan-then-execute flow has two distinct phases:

  1. plan(task)  → ExecutionPlan
     Run a read-only ReAct loop in PLAN permission mode. The LLM explores
     the codebase with read-only tools and emits a markdown checklist, which
     we parse into a structured `ExecutionPlan`.

  2. execute(plan) → str
     Run the approved plan step by step. Plan context is injected into the
     system prompt so the LLM tracks progress; each step's tool call is
     dispatched through the normal `_execute_tool` path.

Originally `AgentEngine.run_plan` and `AgentEngine.run_execute`. Now
decoupled via explicit dependencies — callers (engine) inject the deps
once at construction; tests can substitute fakes for the same seam.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..llm.client import Message
from ..prompts.assembler import PromptAssembler
from ..tools.base import ToolResult, registry
from .permissions import PermissionMode
from .plan import ExecutionPlan


def _log_event(trace_id: str, event: str, **kwargs) -> None:
    """Local re-export of the engine's logger. Keeps the workflow decoupled
    from the engine's logger wiring (it just emits to the same telemetry
    sink via duck-typed call)."""
    # Lazy import to avoid a circular dependency at module load.
    from .engine import _log_event as _engine_log_event

    _engine_log_event(trace_id, event, **kwargs)


class PlanWorkflow:
    """Plan-then-execute workflow with explicit dependency injection.

    Constructor takes the runtime-mutable dependencies once. The two public
    methods (`plan`, `execute`) mirror the previous engine methods, so
    callers (engine, tests) can swap them out without touching call sites.
    """

    def __init__(
        self,
        llm,
        permissions,
        memory,
        context_builder,
        skills,
        config,
        trace_id: str,
        get_current_plan: Callable[[], Any],
        set_current_plan: Callable[[Any], None],
        get_env_context: Callable[[], dict],
        execute_tool: Callable[..., Any],
    ):
        self._llm = llm
        self._permissions = permissions
        self._memory = memory
        self._context_builder = context_builder
        self._skills = skills
        self._config = config
        self._trace_id = trace_id
        self._get_current_plan = get_current_plan
        self._set_current_plan = set_current_plan
        self._get_env_context = get_env_context
        self._execute_tool = execute_tool

    # ── Phase 1: planning ───────────────────────────────────────────

    async def plan(self, task: str) -> ExecutionPlan:
        """Analyze task and produce a structured execution plan.

        Runs the ReAct loop in PLAN permission mode so the LLM can only
        use read-only tools. The final text response is parsed into an
        ExecutionPlan via `ExecutionPlan.from_llm_response`.
        """
        _log_event(self._trace_id, "run_plan_start", task=task[:50])

        original_mode = self._permissions.mode
        self._permissions.mode = PermissionMode.PLAN
        try:
            system = PromptAssembler.build_plan_prompt(
                project_context=self._context_builder.project_context,
                long_term_memory=self._memory.get_long_term_context(),
            )
            self._memory.clear_working_memory()
            self._memory.add("system", system)
            self._memory.add(
                "user",
                f"Create a step-by-step execution plan for this task:\n\n{task}",
            )

            plan_text = ""

            for _step in range(1, self._config.max_steps + 1):
                if self._llm is None:
                    plan_text = f"## Plan: {task}\n\n- [ ] {task}"
                    break

                messages = self._collect_messages()

                response = await self._llm.chat(
                    messages=messages,
                    tools=registry.schemas,
                )

                if isinstance(response, str):
                    plan_text = response
                    break

                if hasattr(response, "tool_calls") and response.tool_calls:
                    for tool_call in response.tool_calls:
                        tool_name = tool_call.function.name
                        try:
                            args = (
                                json.loads(tool_call.function.arguments)
                                if tool_call.function.arguments
                                else {}
                            )
                        except json.JSONDecodeError:
                            args = {}

                        # Plan mode: only read-only tools (enforced by PermissionMode.PLAN)
                        allowed, reason = self._permissions.check(tool_name, args)
                        if not allowed:
                            result = ToolResult(
                                success=False,
                                content="",
                                error=f"Blocked in plan mode: {reason}",
                            )
                        else:
                            tool = registry.get(tool_name)
                            if tool:
                                result = await tool.execute(**args)
                            else:
                                result = ToolResult(
                                    success=False,
                                    content="",
                                    error=f"Unknown tool: {tool_name}",
                                )

                        tool_calls_json = json.dumps(
                            [
                                {
                                    "id": getattr(tool_call, "id", "plan_0"),
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": tool_call.function.arguments,
                                    },
                                }
                            ]
                        )
                        self._memory.add(
                            "assistant",
                            "",
                            tool_calls=tool_calls_json,
                        )
                        self._memory.add(
                            "tool",
                            result.content if result.success else f"Error: {result.error}",
                            tool_call_id=getattr(tool_call, "id", "plan_0"),
                        )
                else:
                    # Text response — plan complete
                    plan_text = response.content if hasattr(response, "content") else str(response)
                    break

            plan = ExecutionPlan.from_llm_response(plan_text, task)
            _log_event(self._trace_id, "run_plan_done", steps=len(plan.steps))
            return plan
        finally:
            self._permissions.mode = original_mode

    # ── Phase 2: execution ──────────────────────────────────────────

    async def execute(self, plan: ExecutionPlan) -> str:
        """Execute an approved plan step by step.

        Injects plan context into the system prompt and runs the normal
        ReAct loop. Tracks step progress in the plan object via the
        engine's `_current_plan` slot (set via the get_current_plan hook).
        """
        _log_event(
            self._trace_id,
            "run_execute_start",
            steps=len(plan.steps),
            summary=plan.summary[:50],
        )
        plan.status = "executing"
        self._set_current_plan(plan)

        plan_md = plan.to_markdown()
        current = plan.current_step().description if plan.current_step() else "all steps complete"
        plan_context = (
            f"Executing plan: {plan.summary}\n" f"{plan_md}\n" f"---\n" f"Current step: {current}\n"
        )

        skill_prompt = self._skills.activate_skills_semantic(plan.task)
        system = self._context_builder.get_system_prompt(
            skill_prompt=skill_prompt,
            plan_context=plan_context,
        )

        self._memory.clear_working_memory()
        self._memory.add("system", system)
        self._memory.add("user", plan.task)

        for _step_num in range(1, self._config.max_steps + 1):
            if self._llm is None:
                return "No LLM configured"

            messages = self._collect_messages()

            # Inject system-reminder into the last user message
            env = self._get_env_context()
            reminder = PromptAssembler.build_system_reminder(**env)
            if reminder:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].role == "user":
                        messages[i] = Message(
                            role="user",
                            content=messages[i].content + "\n\n" + reminder,
                            tool_call_id=messages[i].tool_call_id,
                        )
                        break

            response = await self._llm.chat(
                messages=messages,
                tools=registry.schemas,
            )

            if isinstance(response, str):
                self._memory.add("assistant", response)
                plan.status = "done"
                _log_event(self._trace_id, "run_execute_done", result="text_response")
                return response

            if hasattr(response, "tool_calls") and response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        args = (
                            json.loads(tool_call.function.arguments)
                            if tool_call.function.arguments
                            else {}
                        )
                    except json.JSONDecodeError:
                        args = {}

                    func_args_raw = (
                        tool_call.function.arguments if hasattr(tool_call, "function") else "{}"
                    )
                    await self._execute_tool(
                        tool_name,
                        args,
                        tool_call.id,
                        func_args_raw,
                    )
            else:
                content = response.content if hasattr(response, "content") else str(response)
                self._memory.add("assistant", content)
                plan.status = "done"
                self._set_current_plan(None)
                return content

        plan.status = "done"
        self._set_current_plan(None)
        return (
            f"Task hit step limit ({self._config.max_steps}) — "
            f"you can continue by asking me to pick up where I left off"
        )

    # ── Helpers ─────────────────────────────────────────────────────

    def _collect_messages(self) -> list:
        """Materialize memory into a list of Message objects for the LLM."""
        mem_messages = self._memory.get_messages()
        messages = []
        for m in mem_messages:
            msg = Message(
                role=m.role,
                content=m.content,
                tool_call_id=m.tool_call_id,
            )
            if m.tool_calls:
                msg.tool_calls = m.tool_calls
            messages.append(msg)
        return messages
