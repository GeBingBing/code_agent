"""AskUser tool — structured multi-choice questioning (Claude Code AskUserQuestion style).

Lets the LLM ask the user to choose between approaches when a genuine decision
is needed (several valid paths, an irreversible action, or missing information).
Renders a numbered menu; the user picks by number. This replaces ad-hoc
"应该用 A 还是 B?" prose questions with a scannable, low-effort choice.
"""

from typing import List

from .base import BaseTool, ToolResult, registry


class AskUserTool(BaseTool):
    user_facing_name = "Ask"

    is_concurrency_safe = True
    is_read_only = True
    name = "ask_user"
    description = (
        "Ask the user to choose between options when a genuine decision is needed "
        "(several valid paths, an irreversible action, or missing information). "
        "Use this instead of an open-ended prose question — it gives the user a "
        "scannable numbered menu. Pass 2-4 options, each with a label and a short "
        "description. Set multi_select=true to allow several choices. Only use this "
        "when you truly cannot proceed without the user's input; otherwise just act."
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
                        "question": {
                            "type": "string",
                            "description": "The complete question to ask. Clear and specific.",
                        },
                        "options": {
                            "type": "array",
                            "description": "2-4 mutually exclusive choices (unless multi_select).",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Short display text (1-5 words).",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "What this option means / its trade-offs.",
                                    },
                                },
                                "required": ["label"],
                            },
                            "minItems": 2,
                            "maxItems": 4,
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Allow multiple selections. Default false (single).",
                            "default": False,
                        },
                    },
                    "required": ["question", "options"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        return args.get("question", "")[:60]

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.content:
            return result.content  # "User chose: <label>"
        return super().render_result(result)

    async def execute(
        self,
        question: str,
        options: List[dict],
        multi_select: bool = False,
        **kwargs,
    ) -> ToolResult:
        """Render a numbered menu and read the user's choice.

        Falls back to the confirm_handler's input channel when available so the
        prompt renders consistently with the rest of the CLI.
        """
        # Normalize options — accept {"label": "..."} or bare strings.
        norm = []
        for opt in options:
            if isinstance(opt, str):
                norm.append((opt, ""))
            else:
                norm.append((opt.get("label", "?"), opt.get("description", "")))
        if len(norm) < 2:
            return ToolResult(success=False, content="", error="ask_user needs at least 2 options")

        # Build the menu text.
        lines = [f"\n{question}"]
        for i, (label, desc) in enumerate(norm, 1):
            if desc:
                lines.append(f"  {i}. {label} — {desc}")
            else:
                lines.append(f"  {i}. {label}")
        if multi_select:
            lines.append(
                f"  Enter one or more numbers (1-{len(norm)}, comma-separated), or 0 to cancel: "
            )
        else:
            lines.append(f"  Enter a number (1-{len(norm)}), or 0 to cancel: ")
        prompt = "\n".join(lines)

        # Read input synchronously (same channel as confirm_handler).
        import sys

        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            raw = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            return ToolResult(success=False, content="", error="User cancelled")

        if not raw or raw == "0":
            return ToolResult(success=False, content="", error="User cancelled")

        # Parse the choice(s).
        try:
            picks = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            return ToolResult(success=False, content="", error=f"Invalid input: {raw!r}")

        valid = [p for p in picks if 1 <= p <= len(norm)]
        if not valid:
            return ToolResult(success=False, content="", error=f"No valid choice in: {raw!r}")
        if not multi_select and len(valid) > 1:
            valid = valid[:1]  # single-select → first valid only

        chosen = [norm[p - 1][0] for p in valid]
        answer = ", ".join(chosen)
        return ToolResult(
            success=True, content=f"User chose: {answer}", metadata={"choices": chosen}
        )


registry.register(AskUserTool())
