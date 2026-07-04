"""ToolDispatcher — single tool-call lifecycle owner (PR-23, extracted).

A single tool call goes through several stages:

  0. PlanToolFilter (M1 P0) — hard read-only gate when in plan mode
  1. BEFORE_TOOL_EXECUTION hook  (Ralph TDD, dual-review, audit, OTel)
  2. cwd / parent_run_id injection
  3. Top-level permission check
  4. User confirmation (if required)
  5. Tool-level permission check
  6. Execute + auto-recovery (PR-13)
  7. Append-only log event
  8. Memory record (tool_calls + tool message)
  9. Auto-remember side-effects (last_written_file, etc.)
 10. Plan mode transitions (enter_plan_mode / exit_plan_mode)
 11. AFTER_TOOL_EXECUTION hook

All 11 stages used to be inlined in `AgentEngine._execute_tool`. Pulling
them into `ToolDispatcher` gives:

  - One obvious place to add new stages (e.g., a new hook)
  - A unit-testable seam: tests can pass in a stub `execute` and observe
    the dispatcher's stage order without running real tools
  - Engine shrinks by ~170 lines
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from ..tools.base import ToolResult, registry
from .dual_review import PermissionDenied, ReviewRequiresUser
from .permissions import PermissionMode
from .tdd_state_machine import InvalidTDDTransition

# ── Plan-only tool whitelist (M1 P0) ──────────────────────────────────────────
# When PermissionManager.mode == PLAN, the dispatcher rejects every tool not in
# this set with PlanModeViolation — bypassing the RiskLevel-based filter in
# PermissionManager (which is too coarse: e.g. `grep` defaults to MEDIUM and
# would be wrongly blocked).  Plan transitions (enter/exit) are always allowed
# so the LLM can enter AND exit the mode.  This is the Claude Code behaviour:
# a hard, dispatcher-level gate rather than LLM-impersonated docstrings.

PLAN_ONLY_TOOLS: frozenset = frozenset(
    {
        # ── file / code reads ──────────────────────────────────────────
        "read_file",
        "list_files",
        "grep",
        "glob",
        "code_search",
        "find_references",
        "get_call_graph",
        "lsp",
        # ── spec / docs reads ─────────────────────────────────────────
        "get_spec_status",
        "spec_status",
        "verify_against_spec",
        "verify_spec_acs",
        "web_search",
        "web_fetch",
        # ── project introspection ─────────────────────────────────────
        "git",  # the LLM can use `git status / log / diff / branch --list`
        "list_skills",
        "search_skills",
        "semantic_search",
        "audit_query",
        "metrics_query",
        "logs_query",
        "list_sub_agents",
        # ── plan-mode transitions (always allowed) ────────────────────
        "enter_plan_mode",
        "exit_plan_mode",
        # ── forward-compat: M2 will add ask_user_question here ───────
    }
)


class PlanModeViolation(Exception):
    """Raised by PlanToolFilter when a tool is called outside the plan-only whitelist.

    ToolDispatcher catches this in stage 1 (hook payload) and converts it into
    a ToolResult the LLM can adapt to.  Tests can also assert the exception
    directly when the filter is invoked in isolation.
    """

    def __init__(self, tool_name: str, mode: str):
        self.tool_name = tool_name
        self.mode = mode
        super().__init__(
            f"Plan mode blocks {tool_name}: not in plan-only whitelist. "
            f"Call exit_plan_mode with your plan first, then execute."
        )


def _format_confirm_message(tool_name: str, args: dict) -> str:
    """Format a confirmation message for CLI display — Claude Code style.

    Lifted from AgentEngine._format_confirm_message so ToolDispatcher can
    stay decoupled from the engine module. Re-importing would risk a
    circular import (engine → dispatcher → engine).
    """
    if tool_name == "execute_command":
        return f"Run command: {args.get('command', '')}"
    if tool_name == "write_file":
        return f"Write to: {args.get('path', 'unknown')}"
    if tool_name == "delete_file":
        return f"Delete: {args.get('path', 'unknown')}"
    if tool_name == "install_package":
        pkg = args.get("package", "")
        mgr = args.get("manager", "auto")
        return f"Install {pkg}" + (f" via {mgr}" if mgr != "auto" else "")
    if tool_name == "apply_diff":
        return f"Apply diff to: {args.get('path', 'unknown')}"
    key = next(iter(args)) if args else ""
    val = str(args.get(key, ""))[:60]
    return f"{tool_name}: {key}={val}" if key else tool_name


class ToolDispatcher:
    """Owns the dispatch pipeline for a single tool call.

    Constructor takes runtime-mutable dependencies (hooks, permissions,
    memory) plus a small bundle of accessors for engine state that the
    dispatcher mutates during execution (current_project_dir, plan mode,
    confirm_handler). Using getter/setter lambdas lets tests substitute
    fakes without touching the dispatcher.
    """

    def __init__(
        self,
        hooks,
        event_bus,
        permissions,
        memory,
        trace_id: str,
        workspace: Path,
        get_current_project_dir: Callable[[], Optional[str]],
        set_current_project_dir: Callable[[Optional[str]], None],
        get_pre_plan_mode: Callable[[], Any],
        set_pre_plan_mode: Callable[[Any], None],
        get_confirm_handler: Callable[[], Any],
        get_current_plan: Callable[[], Any],
        log_event: Callable[..., None],
    ):
        self._hooks = hooks
        self._event_bus = event_bus
        self._permissions = permissions
        self._memory = memory
        self._trace_id = trace_id
        self._workspace = workspace
        self._get_current_project_dir = get_current_project_dir
        self._set_current_project_dir = set_current_project_dir
        self._get_pre_plan_mode = get_pre_plan_mode
        self._set_pre_plan_mode = set_pre_plan_mode
        self._get_confirm_handler = get_confirm_handler
        self._get_current_plan = get_current_plan
        self._log_event = log_event

    @staticmethod
    def partition(tool_calls: list) -> tuple:
        """Split a batch into concurrent-safe (reads) and serial (writes).

        Tools marked ``is_concurrency_safe = True`` run in parallel;
        everything else serializes. This is the dispatcher-side of the
        "concurrent tool fan-out" pattern in ``_run_stream_loop``.
        """
        concurrent = []
        serial = []
        for tc in tool_calls:
            func_name = tc.get("name")
            tool = registry.get(func_name)
            if tool and tool.is_concurrency_safe:
                concurrent.append(tc)
            else:
                serial.append(tc)
        return concurrent, serial

    def _check_plan_whitelist(self, func_name: str) -> None:
        """Stage 0: dispatcher-level read-only gate for plan mode.

        In plan mode, only the tools in ``PLAN_ONLY_TOOLS`` are permitted.
        Anything else is rejected with ``PlanModeViolation`` BEFORE we
        spend cycles on hooks, audit, dual-review, or OTel spans.

        Why dispatcher-level rather than relying on
        ``PermissionManager.check()``:
          * The RiskLevel-based filter (``RiskLevel.LOW`` for read tools)
            is too coarse — ``grep``/``glob``/``web_search`` default to
            ``MEDIUM`` and would be wrongly blocked.
          * Docstring-level instructions ("you cannot write files") are
            honoured only by LLM goodwill. The dispatcher is the
            fail-closed enforcement point.

        Plan-mode transitions (``enter_plan_mode``/``exit_plan_mode``) are
        included in the whitelist so the LLM can both enter AND exit the
        mode without bouncing through defaults.
        """
        if self._permissions.mode != PermissionMode.PLAN:
            return
        if func_name in PLAN_ONLY_TOOLS:
            return
        raise PlanModeViolation(func_name, self._permissions.mode.value)

    async def execute(
        self,
        func_name: str,
        args: dict,
        tc_id: str,
        func_args_raw: str,
        assistant_content: str = "",
    ) -> ToolResult:
        """Run one tool call through the full 11-stage pipeline.

        Returns a ``ToolResult`` (success or surfaceable error). Internal
        stage failures are converted into tool errors so the LLM can adapt
        rather than the whole run crashing.

        ``assistant_content`` is the prose the LLM emitted alongside this
        tool call (the narration / intent). It's recorded into memory so the
        next turn sees the full Thought→Action chain, not just the bare
        tool_call — matching Claude Code's ReAct history.
        """
        # Stage 0: PlanToolFilter (M1 P0) — hard read-only gate.
        # Reject any tool outside PLAN_ONLY_TOOLS when in plan mode. Runs
        # BEFORE the BEFORE_TOOL_EXECUTION hook so dual-review / audit /
        # OTel don't pay the cost for calls the dispatcher is going to
        # block anyway. The hook layer remains the right place for
        # plan-mode-aware observability (it sees the ToolResult we return).
        try:
            self._check_plan_whitelist(func_name)
        except PlanModeViolation as exc:
            return ToolResult(
                success=False,
                content="",
                error=str(exc),
                metadata={"plan_blocked": True, "tool": func_name},
            )

        # Stage 0.5: Plan-execution allowed_prompts gate.
        # When executing an approved plan that declared allowed_prompts, the
        # tool must be in that list (Claude Code enforces plan-scoped actions).
        # mark_plan_step is always allowed (it's plan bookkeeping, not a repo
        # action). Skipped when the plan has no allowed_prompts (open plan).
        plan = self._get_current_plan()
        if (
            plan is not None
            and getattr(plan, "status", "") == "executing"
            and getattr(plan, "allowed_prompts", None)
            and func_name != "mark_plan_step"
        ):
            allowed_tools = {ap.tool for ap in plan.allowed_prompts if hasattr(ap, "tool")}
            if allowed_tools and func_name not in allowed_tools:
                return ToolResult(
                    success=False,
                    content="",
                    error=(
                        f"Tool '{func_name}' is not in the approved plan's "
                        f"allowed actions: {sorted(allowed_tools)}. Stick to "
                        f"the plan or exit plan execution."
                    ),
                    metadata={"plan_blocked": True, "tool": func_name},
                )

        # Stage 1: BEFORE_TOOL_EXECUTION hook (Ralph, dual-review, audit, OTel)
        tool_payload = {"tool": func_name, "args": args, "tc_id": tc_id}
        try:
            tool_payload = await self._hooks.execute(
                "before_tool_execution",
                tool_payload,
            )
            args = tool_payload.get("args", args)
        except Exception as hook_exc:
            # Hook raised (Ralph TDD violation, dual-review rejection,
            # etc.) — surface as a tool error so the LLM sees it.
            if isinstance(hook_exc, InvalidTDDTransition):
                return ToolResult(
                    success=False,
                    content="",
                    error=f"TDD violation: {hook_exc}",
                    metadata={"tdd_blocked": True},
                )
            if isinstance(hook_exc, PermissionDenied):
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Dual-agent review rejected: {hook_exc}",
                    metadata={"dual_review_blocked": True},
                )
            if isinstance(hook_exc, ReviewRequiresUser):
                return ToolResult(
                    success=False,
                    content="",
                    error=(f"Dual-agent review split — user adjudication " f"required: {hook_exc}"),
                    metadata={"dual_review_user_required": True},
                )
            raise
        await self._event_bus.emit(
            "before_tool_execution",
            {"tool": func_name, "args": args},
        )

        # Stage 2: cwd / parent_run_id injection
        if func_name == "execute_command" and "cwd" not in args:
            current_dir = self._get_current_project_dir()
            if current_dir:
                args["cwd"] = str(self._workspace / current_dir)
            else:
                args["cwd"] = str(self._workspace)
        if func_name == "spawn_sub_agent" and "parent_run_id" not in args:
            args["parent_run_id"] = self._trace_id
        # Inject the current plan object so mark_plan_step can flip step
        # status without the tool needing a direct engine reference.
        if func_name == "mark_plan_step":
            args["plan"] = self._get_current_plan()

        # ── Assistant tool_calls record (must come BEFORE tool result) ──
        tc_json = json.dumps(
            [
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": func_name, "arguments": func_args_raw},
                }
            ]
        )

        # Stage 3: top-level permission check
        allowed, reason = self._permissions.check(func_name, args)
        if not allowed:
            self._memory.add("assistant", "", tool_calls=tc_json)
            self._memory.add("tool", f"Blocked: {reason}", tool_call_id=tc_id)
            return ToolResult(success=False, content="", error=f"Blocked: {reason}")

        # Stage 4: user confirmation
        if self._permissions.needs_confirmation(func_name, args):
            handler = self._get_confirm_handler()
            if handler:
                message = _format_confirm_message(func_name, args)
                choice = await handler(func_name, message, args)
                if choice == "n":
                    self._memory.add("assistant", "", tool_calls=tc_json)
                    self._memory.add("tool", "User denied", tool_call_id=tc_id)
                    return ToolResult(
                        success=False,
                        content="",
                        error="User denied",
                    )
                if choice == "a":
                    self._permissions.approve_for_session(func_name, args)
            else:
                confirmed = await self._permissions.confirm_async(func_name, args)
                if not confirmed:
                    self._memory.add("assistant", "", tool_calls=tc_json)
                    self._memory.add("tool", "User denied", tool_call_id=tc_id)
                    return ToolResult(
                        success=False,
                        content="",
                        error="User denied",
                    )

        # Stage 5: tool-level permission check
        tool = registry.get(func_name)
        if tool:
            allowed, reason = tool.check_permissions(args)
            if not allowed:
                self._memory.add(
                    "tool",
                    f"Blocked by tool: {reason}",
                    tool_call_id=tc_id,
                )
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Blocked: {reason}",
                )

        # Stage 6: execute + auto-recovery
        if not tool:
            result = ToolResult(
                success=False,
                content="",
                error=f"Unknown tool: {func_name}",
            )
        else:
            try:
                result = await tool.execute(**args)
            except TypeError as e:
                result = ToolResult(
                    success=False,
                    content="",
                    error=(
                        f"Tool argument error: {e}. " f"Required params: check the tool schema."
                    ),
                )
            if not result.success:
                from .error_recovery import recover

                corrected = recover(func_name, args, result.error or "")
                if corrected:
                    result = await tool.execute(**corrected)
                    if result.success:
                        result = ToolResult(
                            success=True,
                            content=f"[Auto-corrected] {result.content}",
                            metadata=result.metadata,
                        )

        # Stage 7: append-only log event
        self._log_event(
            self._trace_id,
            "tool_call",
            tool=func_name,
            path=args.get("path", ""),
            command=args.get("command", "")[:60],
            success=result.success,
            error=result.error or "",
        )

        # Stage 8: memory record — keep the assistant's narration (intent)
        # alongside the tool_call so the next turn sees the full
        # Thought→Action chain, not a bare tool_call with empty content.
        # (Claude Code preserves this; the old code wrote "" here, severing
        # the ReAct thought link.)
        self._memory.add("assistant", assistant_content, tool_calls=tc_json)
        self._memory.add(
            "tool",
            result.content if result.success else f"Error: {result.error}",
            tool_call_id=tc_id,
        )

        # Stage 9: side-effects (project-dir inference only — no mechanical
        # last_X_file/last_command recording; those low-value facts pollute
        # the L3 LRU and crowd out genuinely useful memories. Claude Code
        # doesn't record them either.)
        if result.success:
            if func_name == "write_file":
                path = args.get("path", "")
                if "/" in path and not self._get_current_project_dir():
                    first_seg = path.split("/")[0]
                    if first_seg and first_seg not in (".", "..") and ".." not in first_seg:
                        self._set_current_project_dir(first_seg)

            # Stage 10: plan mode transitions
            if func_name == "enter_plan_mode":
                self._set_pre_plan_mode(self._permissions.mode)
                self._permissions.mode = PermissionMode.PLAN
            elif func_name == "exit_plan_mode":
                # NOTE: stage 10 deliberately does NOT auto-switch mode
                # back to the previous mode here. Pre-090ec3d the
                # plan-mode permission check blocked exit_plan_mode
                # outright, so this code path was unreachable. After
                # 090ec3d let the tool execute, but having stage 10
                # silently flip mode to default caused the agent to
                # start writing files immediately, bypassing the
                # user-visible "approve before executing" gate.
                #
                # The correct flow is now:
                #   1. exit_plan_mode saves the plan to disk
                #   2. CLI renders the plan (Panel)
                #   3. User types "yes" → cli.py auto-approve fires
                #      `_run_async(self._handle_command("/plan accept"))`
                #   4. /plan accept sets mode = default (handled by
                #      agent/commands/builtin.py:_handle_plan)
                #   5. LLM's next iteration now has write tools
                #
                # If we wanted to support auto-mode-flip for users who
                # don't want the manual gate, the right place is an
                # env-var / CLI flag, NOT a silent side-effect here.
                pass

        # Stage 11: AFTER_TOOL_EXECUTION hook
        after_payload = {
            "tool": func_name,
            "args": args,
            "result": result,
            "error": result.error if not result.success else None,
        }
        try:
            await self._hooks.execute("after_tool_execution", after_payload)
        except Exception:
            # Don't let hook errors break the tool return path.
            pass
        await self._event_bus.emit(
            "after_tool_execution",
            {"tool": func_name, "success": result.success},
        )

        return result
