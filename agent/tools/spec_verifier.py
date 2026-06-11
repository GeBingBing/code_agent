"""Spec verification tools — query and update SPECS.md checklist.

Provides tools for the agent to:
- Query current spec status and pending tasks
- Mark tasks as completed
- Verify implementation against spec
"""

from ..core.workspace import WORKSPACE_ROOT as WORKSPACE
from .base import BaseTool, ToolResult, registry


class GetSpecStatusTool(BaseTool):
    user_facing_name = "Spec"

    is_concurrency_safe = True
    is_read_only = True
    name = "get_spec_status"
    description = "Get the current spec status from SPECS.md: active phase, pending tasks, and completed tasks"

    async def execute(self, **kwargs) -> ToolResult:
        from ..core.spec_loader import load_spec

        ctx = load_spec(WORKSPACE)
        if not ctx.phases:
            return ToolResult(success=False, content="", error="No SPECS.md found in workspace")

        lines = ["📋 Spec Status"]
        if ctx.active_phase:
            lines.append(
                f"\nCurrent phase: P{ctx.active_phase.number} — {ctx.active_phase.name} ({ctx.active_phase.status})"
            )
            pending = ctx.active_phase.pending_tasks
            if pending:
                lines.append(f"\nPending tasks ({len(pending)}):")
                for t in pending:
                    lines.append(f"  - [ ] {t.description}")
            done = ctx.active_phase.completed_tasks
            if done:
                lines.append(f"\nCompleted tasks ({len(done)}):")
                for t in done:
                    lines.append(f"  - [x] {t.description}")
        else:
            lines.append("\nNo active phase found.")

        # Show all phases summary
        lines.append("\n---\nAll phases:")
        for p in ctx.phases:
            status_icon = (
                "✅" if p.status == "completed" else "⚠️" if p.status == "partial" else "🔜"
            )
            task_summary = f" ({len(p.completed_tasks)}/{len(p.tasks)} tasks)" if p.tasks else ""
            lines.append(f"  {status_icon} P{p.number}: {p.name}{task_summary}")

        return ToolResult(success=True, content="\n".join(lines))

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }


class MarkSpecTaskDoneTool(BaseTool):
    user_facing_name = "Spec"

    name = "mark_spec_task_done"
    description = "Mark a task as completed in SPECS.md. Provide phase number and task description."

    async def execute(
        self,
        phase_number: int,
        task_description: str,
        **kwargs,
    ) -> ToolResult:
        from ..core.spec_loader import mark_task_done

        success = mark_task_done(WORKSPACE, phase_number, task_description)
        if success:
            return ToolResult(
                success=True,
                content=f"✅ Marked task as done in SPECS.md (Phase {phase_number}): {task_description[:60]}",
            )
        return ToolResult(
            success=False,
            content="",
            error=f"Could not find task '{task_description[:60]}' in Phase {phase_number}",
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
                        "phase_number": {
                            "type": "integer",
                            "description": "Phase number (e.g., 1 for P1-1)",
                        },
                        "task_description": {
                            "type": "string",
                            "description": "Task description to mark as done (partial match supported)",
                        },
                    },
                    "required": ["phase_number", "task_description"],
                },
            },
        }


class VerifyAgainstSpecTool(BaseTool):
    user_facing_name = "Spec"

    is_concurrency_safe = True
    is_read_only = True
    name = "verify_against_spec"
    description = "Verify current implementation against SPECS.md checklist. Provide a summary of what was implemented."

    async def execute(
        self,
        implementation_summary: str = "",
        **kwargs,
    ) -> ToolResult:
        from ..core.spec_loader import verify_against_spec

        report = verify_against_spec(WORKSPACE, implementation_summary)
        if "error" in report:
            return ToolResult(success=False, content="", error=report["error"])

        lines = ["📋 Spec Verification Report"]
        lines.append(f"Coverage: {report['coverage'] * 100:.0f}%")

        if report["completed_tasks"]:
            lines.append(f"\n✅ Completed ({len(report['completed_tasks'])}):")
            for t in report["completed_tasks"][:10]:
                lines.append(f"  - {t}")

        if report["pending_tasks"]:
            lines.append(f"\n🔜 Pending ({len(report['pending_tasks'])}):")
            for t in report["pending_tasks"][:10]:
                lines.append(f"  - {t}")

        if not report["pending_tasks"]:
            lines.append("\n🎉 All spec tasks are complete!")

        return ToolResult(success=True, content="\n".join(lines))

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
                        "implementation_summary": {
                            "type": "string",
                            "description": "Summary of what was implemented (used for heuristic matching)",
                        },
                    },
                },
            },
        }


# Register tools
registry.register(GetSpecStatusTool())
registry.register(MarkSpecTaskDoneTool())
registry.register(VerifyAgainstSpecTool())


# ── PR-06: AC-aware spec tools ────────────────────────────────────


class SpecStatusTool(BaseTool):
    """PR-06: Return AC-aware spec status (active phase, unfinished ACs, progress %)."""

    user_facing_name = "Spec"
    is_concurrency_safe = True
    is_read_only = True
    name = "spec_status"
    description = (
        "Return SPECS.md status: active phase, unfinished acceptance criteria, "
        "and overall progress percentage. Use this to track which ACs are still pending."
    )

    async def execute(self, phase_id: str = "", **kwargs) -> ToolResult:
        from ..core.spec_loader import load_spec_document

        doc = load_spec_document(WORKSPACE)
        if not doc.phases:
            return ToolResult(success=False, content="", error="No SPECS.md found in workspace")

        target = doc.get_phase(phase_id) if phase_id else doc.get_active_phase()
        unfinished = (
            doc.get_unfinished_acs(phase_id=phase_id) if phase_id else doc.get_unfinished_acs()
        )
        prog = doc.progress()

        payload = {
            "active_phase": doc.get_active_phase().id if doc.get_active_phase() else None,
            "queried_phase": target.id if target else None,
            "unfinished_count": len(unfinished),
            "progress_pct": (
                f"{(prog['done'] / prog['total']) * 100:.1f}%" if prog["total"] else "n/a"
            ),
            "progress": prog,
            "unfinished_acs": [ac.to_dict() for ac in unfinished[:10]],
        }
        import json as _json

        return ToolResult(
            success=True,
            content=_json.dumps(payload, indent=2, ensure_ascii=False),
            metadata={
                "active_phase": payload["active_phase"],
                "unfinished_count": payload["unfinished_count"],
            },
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
                        "phase_id": {
                            "type": "string",
                            "description": "Optional phase id to query (e.g. 'P1-2'). Defaults to the active phase.",
                        },
                    },
                },
            },
        }


class MarkAcDoneTool(BaseTool):
    """PR-06: Mark an acceptance criterion as done."""

    user_facing_name = "Spec"
    name = "mark_ac_done"
    description = (
        "Mark an acceptance criterion as done. The completion state is "
        "persisted to `.spec_ac_state.json` in the workspace. "
        "Pass the AC id (e.g. 'P1-2-1') as returned by `spec_status`."
    )

    async def execute(
        self,
        ac_id: str,
        verified_by: str = "agent",
        **kwargs,
    ) -> ToolResult:
        from ..core.spec_loader import mark_ac_done as _mark

        if not ac_id or not ac_id.strip():
            return ToolResult(success=False, content="", error="ac_id is required")
        success = _mark(WORKSPACE, ac_id.strip(), verified_by=verified_by)
        if not success:
            return ToolResult(
                success=False,
                content="",
                error=f"AC id {ac_id!r} not found. Run `spec_status` to list valid ids.",
            )
        return ToolResult(
            success=True,
            content=f"✅ Marked AC {ac_id} done (verified_by={verified_by})",
            metadata={"ac_id": ac_id, "verified_by": verified_by},
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
                        "ac_id": {
                            "type": "string",
                            "description": "AC identifier (e.g. 'P1-2-1') as returned by spec_status.",
                        },
                        "verified_by": {
                            "type": "string",
                            "description": "Who/what verified the AC ('agent' | 'evaluator' | 'human').",
                            "default": "agent",
                            "enum": ["agent", "evaluator", "human"],
                        },
                    },
                    "required": ["ac_id"],
                },
            },
        }


class VerifySpecACSTool(BaseTool):
    """PR-06: Return a gap report (which ACs in a phase remain unfinished)."""

    user_facing_name = "Spec"
    is_concurrency_safe = True
    is_read_only = True
    name = "verify_acs"
    description = (
        "Return a gap report for a SPECS.md phase: which ACs are still pending. "
        "Provide a phase id (e.g. 'P1-2'). If omitted, reports on the active phase."
    )

    async def execute(self, phase_id: str = "", **kwargs) -> ToolResult:
        from ..core.spec_loader import load_spec_document

        doc = load_spec_document(WORKSPACE)
        if not doc.phases:
            return ToolResult(success=False, content="", error="No SPECS.md found in workspace")

        target_id = phase_id or (doc.get_active_phase().id if doc.get_active_phase() else None)
        if not target_id:
            return ToolResult(
                success=False, content="", error="No phase to verify and no active phase"
            )
        target = doc.get_phase(target_id)
        if not target:
            return ToolResult(
                success=False,
                content="",
                error=f"Phase {target_id!r} not found in SPECS.md",
            )
        unfinished = target.pending_acs
        import json as _json

        if not unfinished:
            return ToolResult(
                success=True,
                content=_json.dumps(
                    {
                        "phase": target.id,
                        "title": target.title,
                        "status": "all done",
                        "unfinished_acs": [],
                        "recommendation": f"🎉 All ACs in {target.id} are done.",
                    },
                    indent=2,
                ),
                metadata={"phase": target.id, "unfinished": 0},
            )
        return ToolResult(
            success=True,
            content=_json.dumps(
                {
                    "phase": target.id,
                    "title": target.title,
                    "status": "incomplete",
                    "unfinished_acs": [ac.to_dict() for ac in unfinished],
                    "recommendation": (f"Implement {len(unfinished)} ACs to complete {target.id}."),
                },
                indent=2,
                ensure_ascii=False,
            ),
            metadata={"phase": target.id, "unfinished": len(unfinished)},
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
                        "phase_id": {
                            "type": "string",
                            "description": "Phase id (e.g. 'P1-2'). Defaults to active phase.",
                        },
                    },
                },
            },
        }


# Register PR-06 tools
registry.register(SpecStatusTool())
registry.register(MarkAcDoneTool())
registry.register(VerifySpecACSTool())
