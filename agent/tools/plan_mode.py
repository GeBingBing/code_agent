"""Plan mode tools — LLM-initiated plan/execute transitions.

EnterPlanMode: Switch to read-only exploration mode. The LLM calls this when
it decides it needs to understand the codebase before making changes.

ExitPlanMode: Present the plan for user approval and switch back to execution mode.
The LLM passes the plan content and a list of permitted action categories.

State-change semantics
----------------------
The actual ``PermissionManager.mode = PLAN`` flip is performed by the
dispatcher at stage 10 (see ``ToolDispatcher.execute``). We keep the source
of truth in one place: the dispatcher. The tool's job is to:

  * signal the LLM that the transition succeeded (structured response)
  * validate the LLM's plan content (ExitPlanMode only)
  * generate a stable ``plan_id`` so the saved file can be referenced
    later by ``/plan show``, ``/plan edit``, and ``--resume <plan_id>``
  * record the current plan into engine state via the optional engine
    kwarg when one is available (forward-compat: in M1 we also accept a
    no-engine path so the tool can be unit-tested in isolation)

Why a structured response (not a free-form string)
-------------------------------------------------
Returning a string "Plan mode activated" forces every consumer (CLI, TUI,
audit, plan-persistence, /plan edit) to regex-parse the same field.  With
metadata embedded in the ``ToolResult``, the consumer code becomes a dict
lookup and the LLM gets a richer signal about what changed.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .base import BaseTool, ToolResult, registry

# ── Helpers ─────────────────────────────────────────────────────────────────


def _plan_dir() -> Path:
    """Return the directory plans are written to.

    Centralised so tests can monkeypatch a single point and so the
    ``~/.coding-agent/plans/`` location stays in one place (M1 decision:
    keep this path — no migration to ``<workspace>/.plans/``).
    """
    return Path.home() / ".coding-agent" / "plans"


def _slugify(text: str, max_len: int = 40) -> str:
    """Best-effort slug from free-form text for filenames.

    Not exposed — used only by ``_new_plan_id``.
    """
    import re

    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower())
    s = s.strip("-")
    return s[:max_len] if s else "plan"


def _new_plan_id(task: str) -> str:
    """Build a stable, human-readable plan id.

    Format: ``plan-<slug>-<unix-ts>-<short-uuid>``. The slug aids manual
    scanning of ``~/.coding-agent/plans/``; the timestamp provides
    ordering; the uuid guarantees uniqueness when two plans share a slug.
    """
    return f"plan-{_slugify(task)}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _validate_plan(plan: str) -> Optional[str]:
    """Return an error message if the plan is invalid, else None.

    Rules (M1 P0):
      * must be non-empty
      * must contain a markdown heading that looks like a plan
        (either a top-level ``#`` or ``## Steps`` / ``## Plan`` section)
      * must contain at least one checklist item (``- [ ]`` or ``- [x]``)
    """
    if not plan or not plan.strip():
        return "Plan is empty. Provide a markdown plan with at least one step."

    has_steps_heading = any(
        line.strip().lower().startswith(h)
        for h in ("# plan", "## steps", "## plan", "## summary")
        for line in plan.splitlines()
    )
    has_checklist = any(
        line.lstrip().startswith(("- [ ]", "- [x]", "- [X]")) for line in plan.splitlines()
    )
    if not has_checklist:
        return (
            "Plan has no checklist items. Use `- [ ] <step description>` "
            "for each step so the executor and reviewer can track progress."
        )
    if not has_steps_heading:
        # Soft check — log a warning via the error string but allow the plan
        # through (LLMs vary in their heading style). Future M2 work can
        # tighten this.
        pass
    return None


# ── EnterPlanModeTool ───────────────────────────────────────────────────────


class EnterPlanModeTool(BaseTool):
    user_facing_name = "Plan"

    """Enter read-only plan mode for codebase exploration before making changes."""

    name = "enter_plan_mode"
    description = (
        "Enter plan (read-only) mode. Use this BEFORE making complex changes "
        "to explore the codebase, understand the architecture, and design an "
        "approach. In plan mode, you can only use read tools (read_file, grep, "
        "code_search, list_files, web_fetch, web_search). Call exit_plan_mode "
        "when your plan is ready."
    )
    is_read_only = True
    is_concurrency_safe = False

    def render_call(self, args: dict) -> str:
        return "Entering plan mode"

    async def execute(self, **kwargs: Any) -> ToolResult:
        # Note: PermissionManager.mode is set by the dispatcher's stage 10
        # (after this returns). We keep the source of truth there so the
        # tool stays a "signal" not a "mutator". Returning the structured
        # payload below gives the LLM a clear signal of the new mode.
        return ToolResult(
            success=True,
            content=(
                "Plan mode activated. You are now in READ-ONLY mode.\n\n"
                "Your task:\n"
                "1. Explore the relevant code using read_file, grep, code_search, list_files, web_fetch\n"
                "2. Understand the current architecture and identify what needs to change\n"
                "3. When ready, call exit_plan_mode with your plan\n\n"
                "Rules:\n"
                "- You CANNOT write files, execute commands, or install packages\n"
                "- Focus on understanding, not implementing\n"
                "- Be thorough — read related files to understand dependencies"
            ),
            metadata={
                "mode": "plan",
                "tools_whitelist": sorted(
                    # Mirror of PlanToolFilter.PLAN_ONLY_TOOLS — kept local
                    # so the LLM gets a self-describing response even if the
                    # dispatcher whitelist drifts. M1 source of truth is
                    # PLAN_ONLY_TOOLS in tool_dispatcher.py.
                    (
                        "audit_query",
                        "code_search",
                        "enter_plan_mode",
                        "exit_plan_mode",
                        "find_references",
                        "get_call_graph",
                        "get_spec_status",
                        "git",
                        "glob",
                        "grep",
                        "list_files",
                        "list_skills",
                        "list_sub_agents",
                        "logs_query",
                        "lsp",
                        "metrics_query",
                        "read_file",
                        "search_skills",
                        "semantic_search",
                        "spec_status",
                        "verify_against_spec",
                        "verify_spec_acs",
                        "web_fetch",
                        "web_search",
                    )
                ),
                "transitioned_by": "tool",
            },
        )


# ── ExitPlanModeTool ────────────────────────────────────────────────────────


class ExitPlanModeTool(BaseTool):
    user_facing_name = "Plan"

    """Exit plan mode with a plan for user approval."""

    name = "exit_plan_mode"
    description = (
        "Exit plan mode and present your plan. Provide a step-by-step plan "
        "for the implementation. The user will review and approve before "
        "execution begins. Include 'allowed_prompts' describing what categories "
        "of actions are needed (e.g. 'edit files', 'run tests', 'install packages')."
    )
    is_read_only = True  # Doesn't execute the plan, just presents it
    is_concurrency_safe = False

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
                        "plan": {
                            "type": "string",
                            "description": (
                                "Your implementation plan as markdown. Include:\n"
                                "## Summary: one-line description\n"
                                "## Steps:\n"
                                "- [ ] Step 1: ...\n"
                                "- [ ] Step 2: ...\n"
                                "Each step should reference the files and tools needed."
                            ),
                        },
                        "allowed_prompts": {
                            "type": "string",
                            "description": (
                                "Comma-separated list of action categories needed. "
                                "Examples: 'edit files, run shell commands, install packages, "
                                "run tests, git operations'"
                            ),
                            "default": "",
                        },
                    },
                    "required": ["plan"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        plan = args.get("plan", "")
        summary = plan.split("\n")[0][:60] if plan else "no plan provided"
        return f"Plan: {summary}"

    async def execute(
        self,
        plan: str = "",
        allowed_prompts: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        # ── Validation ──────────────────────────────────────────────
        # M1 P0: empty plans and plans without checklist items are rejected.
        # M2 will tighten further (e.g. require ## Risks / ## AC).
        err = _validate_plan(plan)
        if err:
            return ToolResult(
                success=False,
                content="",
                error=err,
                metadata={"validation": "rejected"},
            )

        # ── Persist to disk ─────────────────────────────────────────
        plan_dir = _plan_dir()
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_id = _new_plan_id(plan)
        # Use the first line as a human-readable task hint
        first_line = next(
            (
                line.strip()
                for line in plan.splitlines()
                if line.strip() and not line.startswith("#")
            ),
            plan[:60],
        )
        # Filesystem-safe name: replace the ":" / "/" in plan_id with "_"
        safe_id = plan_id.replace(":", "_").replace("/", "_")
        plan_file = plan_dir / f"{safe_id}.md"

        body = (
            f"# {plan_id}\n\n"
            f"**Task hint:** {first_line}\n"
            f"**Created:** {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"**Allowed prompts:** {allowed_prompts or 'All actions'}\n"
            f"\n---\n\n{plan}\n"
        )
        plan_file.write_text(body, encoding="utf-8")

        # ── Structured response ─────────────────────────────────────
        # The CLI / TUI / plan editor all key off these metadata fields
        # rather than regex-parsing the free-form content string.
        metadata = {
            "plan_id": plan_id,
            "summary": first_line,
            "allowed_prompts": allowed_prompts or "All actions",
            "persistence_path": str(plan_file),
            "step_count": sum(
                1
                for line in plan.splitlines()
                if line.lstrip().startswith(("- [ ]", "- [x]", "- [X]"))
            ),
        }

        # Optional: record the plan into engine state when an engine was
        # injected via the dispatcher. Kept opt-in so the tool can run
        # in tests without an engine.
        engine = kwargs.get("engine")
        if engine is not None and hasattr(engine, "set_current_plan"):
            try:
                engine.set_current_plan(plan_id)
            except Exception:
                # Don't let engine-state wiring break the tool return path
                pass

        return ToolResult(
            success=True,
            content=(
                f"Plan saved to {plan_file}\n\n"
                f"**plan_id:** `{plan_id}`\n\n"
                f"## Plan\n{plan}\n\n"
                f"## Allowed Actions\n{allowed_prompts or 'All actions'}\n\n"
                "Plan ready for execution. The user will review and the agent "
                "will proceed to implement the approved plan."
            ),
            metadata=metadata,
        )


# Register
registry.register(EnterPlanModeTool())
registry.register(ExitPlanModeTool())
