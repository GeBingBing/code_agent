"""Plan mode tools — LLM-initiated plan/execute transitions.

EnterPlanMode: Switch to read-only exploration mode. The LLM calls this when
it decides it needs to understand the codebase before making changes.

ExitPlanMode: Present the plan for approval and switch back to execution mode.
The LLM passes the plan content and a list of permitted action categories.
"""

from pathlib import Path

from .base import BaseTool, ToolResult, registry


class EnterPlanModeTool(BaseTool):
    user_facing_name = "Plan"

    """Enter read-only plan mode for codebase exploration before making changes."""

    name = "enter_plan_mode"
    description = (
        "Enter plan (read-only) mode. Use this BEFORE making complex changes "
        "to explore the codebase, understand the architecture, and design an "
        "approach. In plan mode, you can only use read tools (read_file, grep, "
        "code_search, list_files). Call exit_plan_mode when your plan is ready."
    )
    is_read_only = True
    is_concurrency_safe = False

    def render_call(self, args: dict) -> str:
        return "Entering plan mode"

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            content=(
                "Plan mode activated. You are now in READ-ONLY mode.\n\n"
                "Your task:\n"
                "1. Explore the relevant code using read_file, grep, code_search, list_files\n"
                "2. Understand the current architecture and identify what needs to change\n"
                "3. When ready, call exit_plan_mode with your plan\n\n"
                "Rules:\n"
                "- You CANNOT write files, execute commands, or install packages\n"
                "- Focus on understanding, not implementing\n"
                "- Be thorough — read related files to understand dependencies\n"
            ),
        )


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

    async def execute(self, plan: str = "", allowed_prompts: str = "", **kwargs) -> ToolResult:
        if not plan.strip():
            return ToolResult(
                success=False,
                content="",
                error="No plan provided. Please include your implementation plan.",
            )

        # Write plan to file for persistence
        plan_dir = Path.home() / ".coding-agent" / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        import time

        plan_file = plan_dir / f"plan_{int(time.time())}.md"
        plan_content = (
            f"# Plan\n\n{plan}\n\n## Allowed Actions\n\n{allowed_prompts or 'All actions'}"
        )
        plan_file.write_text(plan_content, encoding="utf-8")

        return ToolResult(
            success=True,
            content=(
                f"Plan saved to {plan_file}\n\n"
                f"## Plan\n{plan}\n\n"
                f"## Allowed Actions\n{allowed_prompts or 'All actions'}\n\n"
                "Plan ready for execution. The user will review and the agent "
                "will proceed to implement the approved plan."
            ),
        )


# Register
registry.register(EnterPlanModeTool())
registry.register(ExitPlanModeTool())
