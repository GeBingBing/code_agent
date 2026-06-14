"""Execution Plan — structured task decomposition for plan-then-execute workflow.

Plan-then-Execute 是 Hermes（结构化推理）+ specDD（规格驱动）的融合：
1. run_plan()  — 分析需求，生成结构化 ExecutionPlan
2. run_execute() — 按计划逐步执行，每步完成后验证
3. run() — 组合上述两个阶段

M2 schema additions (all OPTIONAL, defaulting to empty / None — fully
backwards-compatible with M1 plans persisted to ~/.coding-agent/plans/):

  ExecutionPlan:
    - plan_id, title, revision, parent_plan_id
    - risks: list[Risk]
    - alternatives: list[Alternative]
    - acceptance_criteria: list[AC]
    - allowed_prompts: list[AllowedPrompt]
    - review_notes: str

  PlanStep:
    - tdd_phase: TDDState | None
    - step_risk: RiskLevel | None
    - dependencies: list[int]   (other step ids this one blocks on)
    - verify_command: str
    - estimated_complexity: str  ("S" | "M" | "L" | "XL")

The M2 markdown format also recognises four new sections:

  ## Risks
    - category: severity — mitigation
  ## Alternatives
    - description — why_rejected
  ## Acceptance Criteria
    - id: description (verify_command)
  ## Allowed Actions
    - tool — justification (risk_level)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .permissions import RiskLevel
from .tdd_state_machine import TDDState


@dataclass
class Risk:
    """A risk the plan acknowledges, with severity and mitigation."""

    category: str
    severity: str = "medium"
    mitigation: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "mitigation": self.mitigation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Risk":
        return cls(
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            mitigation=d.get("mitigation", ""),
        )

    def to_markdown_line(self) -> str:
        prefix = f"- **{self.category}** ({self.severity})"
        if self.mitigation:
            return f"{prefix} — {self.mitigation}"
        return prefix


@dataclass
class Alternative:
    description: str
    trade_offs: str = ""
    why_rejected: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "trade_offs": self.trade_offs,
            "why_rejected": self.why_rejected,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Alternative":
        return cls(
            description=d.get("description", ""),
            trade_offs=d.get("trade_offs", ""),
            why_rejected=d.get("why_rejected", ""),
        )

    def to_markdown_line(self) -> str:
        parts = [f"- {self.description}"]
        if self.why_rejected:
            parts.append(f"  - rejected: {self.why_rejected}")
        if self.trade_offs:
            parts.append(f"  - trade-offs: {self.trade_offs}")
        return "\n".join(parts)


@dataclass
class AC:
    """An acceptance criterion for the plan as a whole (vs per-step)."""

    id: str
    description: str
    verify_command: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "verify_command": self.verify_command,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AC":
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            verify_command=d.get("verify_command", ""),
        )

    def to_markdown_line(self) -> str:
        head = f"- **{self.id}**: {self.description}"
        if self.verify_command:
            return f"{head} — verify: `{self.verify_command}`"
        return head


@dataclass
class AllowedPrompt:
    tool: str
    justification: str = ""
    risk_level: str = "medium"

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "justification": self.justification,
            "risk_level": self.risk_level,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AllowedPrompt":
        return cls(
            tool=d.get("tool", ""),
            justification=d.get("justification", ""),
            risk_level=d.get("risk_level", "medium"),
        )

    def to_markdown_line(self) -> str:
        head = f"- {self.tool} ({self.risk_level} risk)"
        if self.justification:
            return f"{head} — {self.justification}"
        return head


@dataclass
class PlanStep:
    id: int
    description: str
    tool_hint: str = ""
    expected_outcome: str = ""
    status: str = "pending"
    result: str = ""
    tdd_phase: Optional[TDDState] = None
    step_risk: Optional[RiskLevel] = None
    dependencies: List[int] = field(default_factory=list)
    verify_command: str = ""
    estimated_complexity: str = ""

    def to_markdown(self) -> str:
        return f"- [ ] {self.description}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "tool_hint": self.tool_hint,
            "expected_outcome": self.expected_outcome,
            "status": self.status,
            "result": self.result,
            "tdd_phase": self.tdd_phase.value if self.tdd_phase else None,
            "step_risk": self.step_risk.value if self.step_risk else None,
            "dependencies": list(self.dependencies),
            "verify_command": self.verify_command,
            "estimated_complexity": self.estimated_complexity,
        }


@dataclass
class ExecutionPlan:
    task: str
    steps: List[PlanStep]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"
    summary: str = ""
    plan_id: str = ""
    title: str = ""
    revision: int = 1
    parent_plan_id: str = ""
    risks: List[Risk] = field(default_factory=list)
    alternatives: List[Alternative] = field(default_factory=list)
    acceptance_criteria: List[AC] = field(default_factory=list)
    allowed_prompts: List[AllowedPrompt] = field(default_factory=list)
    review_notes: str = ""

    def to_markdown(self) -> str:
        title = self.title or self.summary or self.task[:80]
        lines = [f"## Plan: {title}", ""]
        meta_bits = [
            f"**Plan ID:** `{self.plan_id}`" if self.plan_id else None,
            f"**Revision:** {self.revision}" if self.revision > 1 else None,
            f"**Status:** {self.status}",
            f"**Steps:** {len(self.steps)}",
            f"**Created:** {self.created_at[:19]}",
        ]
        lines.append("  \n".join(b for b in meta_bits if b) + "  ")
        lines.append("")

        for step in self.steps:
            status_icon = {
                "pending": "○",
                "in_progress": "◉",
                "done": "✓",
                "skipped": "−",
                "failed": "✗",
            }
            icon = status_icon.get(step.status, "?")
            tag_bits = []
            if step.tdd_phase:
                tag_bits.append(f"tdd:{step.tdd_phase.value}")
            if step.step_risk:
                tag_bits.append(f"risk:{step.step_risk.value}")
            if step.estimated_complexity:
                tag_bits.append(f"size:{step.estimated_complexity}")
            tag_str = f" `{','.join(tag_bits)}`" if tag_bits else ""
            lines.append(f"- [{icon}] **{step.description}**{tag_str}")
            if step.tool_hint:
                lines.append(f"  → tool: `{step.tool_hint}`")
            if step.expected_outcome:
                lines.append(f"  → expect: {step.expected_outcome}")
            if step.dependencies:
                deps = ", ".join(f"#{d}" for d in step.dependencies)
                lines.append(f"  → depends on: {deps}")
            if step.verify_command:
                lines.append(f"  → verify: `{step.verify_command}`")
            if step.result and step.status in ("done", "failed"):
                lines.append(f"  → result: {step.result}")
        lines.append("")

        if self.acceptance_criteria:
            lines.append("## Acceptance Criteria")
            for ac in self.acceptance_criteria:
                lines.append(ac.to_markdown_line())
            lines.append("")
        if self.risks:
            lines.append("## Risks")
            for r in self.risks:
                lines.append(r.to_markdown_line())
            lines.append("")
        if self.alternatives:
            lines.append("## Alternatives Considered")
            for alt in self.alternatives:
                lines.append(alt.to_markdown_line())
            lines.append("")
        if self.allowed_prompts:
            lines.append("## Allowed Actions")
            for ap in self.allowed_prompts:
                lines.append(ap.to_markdown_line())
            lines.append("")
        if self.review_notes:
            lines.append("## Review Notes")
            lines.append(self.review_notes)
            lines.append("")

        dep_steps = [s for s in self.steps if s.dependencies]
        if dep_steps:
            lines.append("## Dependency Graph")
            lines.append("```mermaid")
            lines.append("graph TD")
            for s in self.steps:
                lines.append(f'    S{s.id}["{s.description[:40]}"]:::pending')
            for s in dep_steps:
                for dep_id in s.dependencies:
                    lines.append(f"    S{dep_id} --> S{s.id}")
            lines.append("```")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "status": self.status,
            "summary": self.summary,
            "plan_id": self.plan_id,
            "title": self.title,
            "revision": self.revision,
            "parent_plan_id": self.parent_plan_id,
            "risks": [r.to_dict() for r in self.risks],
            "alternatives": [a.to_dict() for a in self.alternatives],
            "acceptance_criteria": [ac.to_dict() for ac in self.acceptance_criteria],
            "allowed_prompts": [ap.to_dict() for ap in self.allowed_prompts],
            "review_notes": self.review_notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionPlan":
        steps_raw = d.get("steps", [])
        steps = []
        for s in steps_raw:
            tdd = s.get("tdd_phase")
            risk = s.get("step_risk")
            steps.append(
                PlanStep(
                    id=s.get("id", 0),
                    description=s.get("description", ""),
                    tool_hint=s.get("tool_hint", ""),
                    expected_outcome=s.get("expected_outcome", ""),
                    status=s.get("status", "pending"),
                    result=s.get("result", ""),
                    tdd_phase=TDDState(tdd) if tdd else None,
                    step_risk=RiskLevel(risk) if risk else None,
                    dependencies=list(s.get("dependencies", [])),
                    verify_command=s.get("verify_command", ""),
                    estimated_complexity=s.get("estimated_complexity", ""),
                )
            )
        return cls(
            task=d.get("task", ""),
            steps=steps,
            created_at=d.get("created_at", datetime.now().isoformat()),
            status=d.get("status", "pending"),
            summary=d.get("summary", ""),
            plan_id=d.get("plan_id", ""),
            title=d.get("title", ""),
            revision=d.get("revision", 1),
            parent_plan_id=d.get("parent_plan_id", ""),
            risks=[Risk.from_dict(r) for r in d.get("risks", [])],
            alternatives=[Alternative.from_dict(a) for a in d.get("alternatives", [])],
            acceptance_criteria=[AC.from_dict(ac) for ac in d.get("acceptance_criteria", [])],
            allowed_prompts=[AllowedPrompt.from_dict(ap) for ap in d.get("allowed_prompts", [])],
            review_notes=d.get("review_notes", ""),
        )

    @classmethod
    def from_llm_response(cls, text: str, task: str) -> "ExecutionPlan":
        steps: List[PlanStep] = []
        step_id = 0

        section_names = (
            "## Risks",
            "## Alternatives Considered",
            "## Alternatives",
            "## Acceptance Criteria",
            "## AC",
            "## Allowed Actions",
            "## Review Notes",
        )
        section_starts: list = []
        for line in text.split("\n"):
            stripped = line.strip()
            for name in section_names:
                if stripped == name or stripped.startswith(name + " "):
                    section_starts.append((name, line))

        if section_starts:
            cut_idx = text.split("\n").index(section_starts[0][1])
            body_text = "\n".join(text.split("\n")[:cut_idx])
        else:
            body_text = text

        for line in body_text.split("\n"):
            stripped = line.strip()
            match = re.match(r"-\s*\[([ xX])\]\s+(.+)", stripped)
            if match:
                step_id += 1
                desc = match.group(2).strip()
                tool_hint = ""
                tool_match = re.search(r"`(\w+)`", desc)
                if tool_match:
                    tool_hint = tool_match.group(1)

                steps.append(
                    PlanStep(
                        id=step_id,
                        description=desc,
                        tool_hint=tool_hint,
                    )
                )

        if not steps:
            summary = text.strip().split("\n")[0][:120] if text.strip() else task
            steps.append(
                PlanStep(
                    id=1,
                    description=task,
                    tool_hint="",
                    expected_outcome=summary,
                )
            )

        summary = ""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("##") or stripped.startswith("# "):
                summary = stripped.lstrip("#").strip()
                break
        if not summary:
            first_line = text.strip().split("\n")[0] if text.strip() else task
            summary = first_line[:80]

        plan = cls(
            task=task,
            steps=steps,
            summary=summary,
        )
        plan._parse_sections(text)
        return plan

    def _parse_sections(self, text: str) -> None:
        sections = _split_into_sections(text)
        self.risks = [_parse_risk_line(line) for line in sections.get("Risks", [])]
        self.risks = [r for r in self.risks if r is not None]
        self.alternatives = [
            _parse_alt_line(line) for line in sections.get("Alternatives Considered", [])
        ]
        if not self.alternatives:
            self.alternatives = [_parse_alt_line(line) for line in sections.get("Alternatives", [])]
        self.alternatives = [a for a in self.alternatives if a is not None]

        ac_lines = sections.get("Acceptance Criteria", []) or sections.get("AC", [])
        self.acceptance_criteria = [_parse_ac_line(line) for line in ac_lines]
        self.acceptance_criteria = [ac for ac in self.acceptance_criteria if ac is not None]

        self.allowed_prompts = [
            _parse_allowed_line(line) for line in sections.get("Allowed Actions", [])
        ]
        self.allowed_prompts = [ap for ap in self.allowed_prompts if ap is not None]

        review = sections.get("Review Notes", [])
        if review:
            self.review_notes = "\n".join(review).strip()

    def current_step(self) -> Optional[PlanStep]:
        for step in self.steps:
            if step.status in ("pending", "in_progress"):
                return step
        return None

    def progress(self) -> str:
        done = sum(1 for s in self.steps if s.status == "done")
        return f"{done}/{len(self.steps)} done"

    def is_complete(self) -> bool:
        return all(s.status in ("done", "skipped") for s in self.steps)


def _split_into_sections(text: str) -> dict:
    sections: dict = {}
    current_name: Optional[str] = None
    for line in text.split("\n"):
        if line.startswith("## "):
            current_name = line[3:].strip()
            sections.setdefault(current_name, [])
        elif current_name is not None:
            sections[current_name].append(line)
    return sections


_RISK_LINE_RE = re.compile(
    r"^-\s+(?:\*\*)?(?P<category>[^()]+?)(?:\*\*)?\s*"
    r"\((?P<severity>low|medium|high|critical)\)"
    r"(?:\s*[—\-]\s*(?P<mitigation>.+))?$"
)


def _parse_risk_line(line: str) -> Optional[Risk]:
    line = line.strip()
    if not line or not line.startswith("-"):
        return None
    m = _RISK_LINE_RE.match(line)
    if not m:
        return Risk(category=line[2:].strip(), severity="medium")
    return Risk(
        category=m.group("category").strip(),
        severity=m.group("severity"),
        mitigation=(m.group("mitigation") or "").strip(),
    )


def _parse_alt_line(line: str) -> Optional[Alternative]:
    line = line.rstrip()
    if not line or not line.startswith("-"):
        return None
    desc = line[1:].strip()
    return Alternative(description=desc, why_rejected="", trade_offs="")


_AC_LINE_RE = re.compile(
    r"^-\s+(?:\*\*)?(?P<id>AC-[A-Za-z0-9_-]+)(?:\*\*)?:\s*"
    r"(?P<description>.+?)(?:\s*[—\-]\s*verify:\s*`(?P<verify>[^`]+)`\s*)?$"
)


def _parse_ac_line(line: str) -> Optional[AC]:
    line = line.strip()
    if not line or not line.startswith("-"):
        return None
    m = _AC_LINE_RE.match(line)
    if not m:
        return None
    return AC(
        id=m.group("id"),
        description=m.group("description").strip(),
        verify_command=(m.group("verify") or "").strip(),
    )


_ALLOWED_LINE_RE = re.compile(
    r"^-\s+(?P<tool>[A-Za-z0-9_]+)\s*"
    r"\((?P<risk>low|medium|high)\s*risk\)"
    r"(?:\s*[—\-]\s*(?P<justification>.+))?$"
)


def _parse_allowed_line(line: str) -> Optional[AllowedPrompt]:
    line = line.strip()
    if not line or not line.startswith("-"):
        return None
    m = _ALLOWED_LINE_RE.match(line)
    if not m:
        return AllowedPrompt(tool=line[2:].strip(), risk_level="medium")
    return AllowedPrompt(
        tool=m.group("tool"),
        risk_level=m.group("risk"),
        justification=(m.group("justification") or "").strip(),
    )
