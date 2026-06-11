"""Execution Plan — structured task decomposition for plan-then-execute workflow.

Plan-then-Execute 是 Hermes（结构化推理）+ specDD（规格驱动）的融合：
1. run_plan()  — 分析需求，生成结构化 ExecutionPlan
2. run_execute() — 按计划逐步执行，每步完成后验证
3. run() — 组合上述两个阶段
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class PlanStep:
    """A single step in the execution plan."""

    id: int
    description: str          # 步骤描述
    tool_hint: str = ""       # 预期工具，如 "write_file"
    expected_outcome: str = ""  # 预期结果
    status: str = "pending"   # pending | in_progress | done | skipped | failed
    result: str = ""          # 执行后的结果摘要

    def to_markdown(self) -> str:
        """Render as a single markdown checklist item."""
        return f"- [ ] {self.description}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "description": self.description,
            "tool_hint": self.tool_hint, "expected_outcome": self.expected_outcome,
            "status": self.status, "result": self.result,
        }


@dataclass
class ExecutionPlan:
    """A structured plan for task execution."""

    task: str
    steps: List[PlanStep]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"  # pending | confirmed | executing | done | failed
    summary: str = ""        # 一句话概述

    def to_markdown(self) -> str:
        """Render plan as markdown for display."""
        lines = [
            f"## Plan: {self.summary or self.task[:80]}",
            f"",
            f"**Status:** {self.status}  \n**Steps:** {len(self.steps)} | **Created:** {self.created_at[:19]}",
            f"",
        ]
        for step in self.steps:
            status_icon = {"pending": "○", "in_progress": "◉", "done": "✓", "skipped": "−", "failed": "✗"}
            icon = status_icon.get(step.status, "?")
            lines.append(f"- [{icon}] **{step.description}**")
            if step.tool_hint:
                lines.append(f"  → tool: `{step.tool_hint}`")
            if step.expected_outcome:
                lines.append(f"  → expect: {step.expected_outcome}")
            if step.result and step.status in ("done", "failed"):
                lines.append(f"  → result: {step.result}")
        return "\n".join(lines)

    @classmethod
    def from_llm_response(cls, text: str, task: str) -> "ExecutionPlan":
        """Parse an LLM response into an ExecutionPlan.

        Extracts markdown checklist items (lines starting with '- [ ]' or '- [x]')
        and converts them into PlanStep objects.
        """
        steps: List[PlanStep] = []
        step_id = 0

        # Look for checklist lines in markdown
        for line in text.split("\n"):
            stripped = line.strip()
            # Match "- [ ] description" or "- [x] description"
            match = re.match(r'-\s*\[([ xX])\]\s+(.+)', stripped)
            if match:
                step_id += 1
                desc = match.group(2).strip()
                # Extract tool hint if present: "Use write_file to create X"
                tool_hint = ""
                tool_match = re.search(r'`(\w+)`', desc)
                if tool_match:
                    tool_hint = tool_match.group(1)

                steps.append(PlanStep(
                    id=step_id,
                    description=desc,
                    tool_hint=tool_hint,
                ))

        # Fallback: if no checklist found, create single-step plan from first sentence
        if not steps:
            summary = text.strip().split("\n")[0][:120] if text.strip() else task
            steps.append(PlanStep(
                id=1,
                description=task,
                tool_hint="",
                expected_outcome=summary,
            ))

        # Derive summary from first line or task
        summary = ""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("##") or stripped.startswith("# "):
                summary = stripped.lstrip("#").strip()
                break
        if not summary:
            first_line = text.strip().split("\n")[0] if text.strip() else task
            summary = first_line[:80]

        return cls(
            task=task,
            steps=steps,
            summary=summary,
        )

    def current_step(self) -> Optional[PlanStep]:
        """Return the first pending or in-progress step."""
        for step in self.steps:
            if step.status in ("pending", "in_progress"):
                return step
        return None

    def progress(self) -> str:
        """Return progress string like '3/5 done'."""
        done = sum(1 for s in self.steps if s.status == "done")
        return f"{done}/{len(self.steps)} done"

    def is_complete(self) -> bool:
        """Check if all steps are done (or skipped)."""
        return all(s.status in ("done", "skipped") for s in self.steps)
