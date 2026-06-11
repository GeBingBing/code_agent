"""Evaluator Agent (PR-09).

An independent "judge" agent that scores completed tasks on 4 dimensions:
completion, code_quality, security, performance. Outputs both human-readable
SCORE.md and machine-readable .score.json.

Why independent?
- 1.md §9: an independent evaluator avoids reinforcing the executing
  agent's own biases ("I think my code is great").
- Default uses a *different* model from the main engine when possible
  (Claude judges GPT output, GPT judges Claude output), to reduce
  in-family bias.

Evidence sources (preferred to ad-hoc judgment):
- Audit log (PR-08) — what tools were called, what was denied, durations
- Git diff stat — actual code changes made
- Test results — pass/fail from `run_tests` calls in audit
- Acceptance criteria (PR-06) — explicit "done" definition
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


DIMENSIONS = ("completion", "code_quality", "security", "performance")


@dataclass
class EvaluationScore:
    """A single dimension score (0-10)."""

    dimension: str
    score: float
    rationale: str = ""

    def __post_init__(self):
        # Clamp to 0-10 — LLMs sometimes return 11 or -1
        try:
            self.score = max(0.0, min(10.0, float(self.score)))
        except (TypeError, ValueError):
            self.score = 0.0


@dataclass
class EvaluationReport:
    """Complete evaluation report — scores + findings + suggestions."""

    task: str
    agent_id: str
    scores: List[EvaluationScore]
    findings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    overall_score: float = 0.0
    evaluated_at: str = ""
    model: str = ""

    def __post_init__(self):
        if not self.evaluated_at:
            self.evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.scores and self.overall_score == 0.0:
            # Auto-compute mean only when not pre-set
            self.overall_score = sum(s.score for s in self.scores) / len(self.scores)

    @property
    def total(self) -> float:
        return self.overall_score

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "agent_id": self.agent_id,
            "scores": [asdict(s) for s in self.scores],
            "findings": list(self.findings),
            "suggestions": list(self.suggestions),
            "overall_score": round(self.overall_score, 2),
            "evaluated_at": self.evaluated_at,
            "model": self.model,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        lines = [
            "# Task Evaluation",
            f"- Task: {self.task}",
            f"- Agent: {self.agent_id}",
            f"- Evaluated at: {self.evaluated_at}",
            f"- Evaluator model: {self.model or 'unknown'}",
            "",
            "## Scores",
        ]
        for s in self.scores:
            lines.append(f"- **{s.dimension}**: {s.score:.1f}/10 — {s.rationale}")
        lines.append(f"- **总分**: {self.overall_score:.1f}/10")
        lines.append("")
        if self.findings:
            lines.append("## Findings")
            for f in self.findings:
                lines.append(f"- {f}")
            lines.append("")
        if self.suggestions:
            lines.append("## 建议改进")
            for s in self.suggestions:
                lines.append(f"- {s}")
            lines.append("")
        return "\n".join(lines)


# ── Evaluator ────────────────────────────────────────────────────


class EvaluatorAgent:
    """Independent evaluator that produces an EvaluationReport.

    Usage:
        evaluator = EvaluatorAgent(engine)  # uses engine.llm
        report = await evaluator.evaluate(task="implement X", agent_id="main")
        evaluator.write_report(report, workspace=Path("/workspace"))
    """

    def __init__(self, engine, model: Optional[str] = None):
        self.engine = engine
        self.model = model or self._pick_alternate_model()

    def _pick_alternate_model(self) -> str:
        """Choose a model from a different family than the engine's main model.

        This is a heuristic: if the main model is GPT, prefer Claude (and
        vice-versa). If we can't tell, fall back to the main model — better
        a same-family judge than no judge at all.
        """
        try:
            main = (self.engine.config.model or "").lower()
        except AttributeError:
            return ""
        if "gpt" in main or "openai" in main:
            return "claude-sonnet-4-6"
        if "claude" in main or "anthropic" in main:
            return "gpt-4o"
        return main or ""

    # ── Public entrypoint ─────────────────────────────────────────

    async def evaluate(
        self,
        task: str,
        agent_id: str = "main",
        audit_records: Optional[List[dict]] = None,
        workspace: Optional[Path] = None,
    ) -> EvaluationReport:
        """Run the full evaluation pipeline."""
        evidence = self._gather_evidence(
            task=task,
            agent_id=agent_id,
            audit_records=audit_records,
            workspace=workspace,
        )
        if self.engine is not None and getattr(self.engine, "llm", None) is not None:
            try:
                scores, findings, suggestions = await self._score_with_llm(evidence)
            except Exception as e:
                logger.warning("Evaluator LLM call failed, using fallback: %s", e)
                scores, findings, suggestions = self._score_heuristic(evidence)
        else:
            scores, findings, suggestions = self._score_heuristic(evidence)

        return EvaluationReport(
            task=task,
            agent_id=agent_id,
            scores=scores,
            findings=findings,
            suggestions=suggestions,
            model=self.model,
        )

    # ── Evidence collection ──────────────────────────────────────

    def _gather_evidence(
        self,
        task: str,
        agent_id: str,
        audit_records: Optional[List[dict]] = None,
        workspace: Optional[Path] = None,
    ) -> dict:
        """Collect objective evidence: audit log stats, git diff, test results."""
        audit = audit_records or []
        evidence: dict = {
            "task": task,
            "agent_id": agent_id,
            "tool_calls": sum(1 for r in audit if r.get("action") == "tool_call"),
            "tool_results": sum(1 for r in audit if r.get("action") == "tool_result"),
            "errors": [
                {"tool": r.get("tool"), "error": r.get("error")} for r in audit if r.get("error")
            ][
                :20
            ],  # Cap to keep prompt size sane
            "permission_decisions": {
                "allow": sum(1 for r in audit if r.get("permission_decision") == "allow"),
                "ask": sum(1 for r in audit if r.get("permission_decision") == "ask"),
                "deny": sum(1 for r in audit if r.get("permission_decision") == "deny"),
            },
            "tools_used": _count(r.get("tool") for r in audit if r.get("tool")),
        }
        # Git diff (best-effort; never fail evaluation if git is missing)
        if workspace and Path(workspace).exists():
            try:
                stat = subprocess.run(
                    ["git", "diff", "--stat", "HEAD"],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if stat.returncode == 0:
                    evidence["git_diff_stat"] = stat.stdout[:2000]
                diff = subprocess.run(
                    ["git", "diff", "HEAD"],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if diff.returncode == 0:
                    evidence["git_diff_preview"] = diff.stdout[:8000]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
        # Test runs — extract pass/fail signal
        test_records = [r for r in audit if r.get("tool") == "run_tests"]
        if test_records:
            evidence["test_runs"] = len(test_records)
            last = test_records[-1]
            evidence["last_test_outcome"] = {
                "duration_ms": last.get("duration_ms"),
                "error": last.get("error"),
            }
        return evidence

    # ── Scoring (LLM) ────────────────────────────────────────────

    async def _score_with_llm(self, evidence: dict):
        """Ask the LLM to produce 4 dimension scores + findings + suggestions."""
        from ..llm.client import Message  # local import to avoid hard dep at module load

        prompt = self._build_prompt(evidence)
        chat_kwargs = {}
        if self.model:
            chat_kwargs["model"] = self.model
        response, _ = await self.engine.llm.chat(
            [Message(role="user", content=prompt)],
            **chat_kwargs,
        )
        data = _parse_score_response(response)
        scores = [
            EvaluationScore(
                dimension=s.get("dimension", "unknown"),
                score=s.get("score", 0),
                rationale=s.get("rationale", ""),
            )
            for s in data.get("scores", [])
        ]
        # Ensure all 4 dimensions are present — fill missing with 0/no-rationale
        seen = {s.dimension for s in scores}
        for dim in DIMENSIONS:
            if dim not in seen:
                scores.append(EvaluationScore(dimension=dim, score=0.0, rationale="no signal"))
        findings = list(data.get("findings", []))[:20]
        suggestions = list(data.get("suggestions", []))[:20]
        return scores, findings, suggestions

    def _build_prompt(self, evidence: dict) -> str:
        evidence_json = json.dumps(evidence, indent=2, ensure_ascii=False, default=str)
        # Truncate prompt to ~20KB to avoid blowing the LLM context
        if len(evidence_json) > 20000:
            evidence_json = evidence_json[:20000] + "\n...[truncated]"
        return (
            "You are an independent code-quality evaluator. Score the following\n"
            "agent run on 4 dimensions (0-10 each):\n\n"
            "1. completion    — did the agent achieve the task? AC met?\n"
            "2. code_quality  — clean, idiomatic, well-tested?\n"
            "3. security      — vulnerabilities? input validation?\n"
            "4. performance   — bottlenecks? algorithmic complexity?\n\n"
            "For each dimension produce a score (0-10) and a 1-2 sentence rationale\n"
            "that cites specific evidence below.\n\n"
            "## Evidence\n"
            f"{evidence_json}\n\n"
            "## Output (JSON only, no prose)\n"
            '{"scores":[{"dimension":"completion","score":<0-10>,"rationale":"..."},'
            '{"dimension":"code_quality","score":<0-10>,"rationale":"..."},'
            '{"dimension":"security","score":<0-10>,"rationale":"..."},'
            '{"dimension":"performance","score":<0-10>,"rationale":"..."}],'
            '"findings":["...","..."],"suggestions":["...","..."]}'
        )

    # ── Scoring (heuristic fallback) ─────────────────────────────

    def _score_heuristic(self, evidence: dict):
        """No-LLM fallback — deterministic scoring from evidence shape.

        Used when the engine has no LLM (test mode) or the LLM call fails.
        Better than crashing or returning all-zeros.
        """
        n_tools = evidence.get("tool_calls", 0)
        n_errors = len(evidence.get("errors", []))
        denies = evidence.get("permission_decisions", {}).get("deny", 0)
        # Completion: penalised by errors and denied actions
        completion = max(0.0, min(10.0, 10.0 - 1.5 * n_errors - 2.0 * denies))
        # Code quality: penalised by lots of retries / errors
        code_quality = max(0.0, min(10.0, 10.0 - n_errors * 0.5))
        # Security: penalised heavily by denied actions (often dangerous calls)
        security = max(0.0, min(10.0, 10.0 - 2.0 * denies))
        # Performance: neutral 7 if we have no signal
        performance = 7.0 if n_tools > 0 else 5.0
        scores = [
            EvaluationScore("completion", completion, f"{n_errors} errors, {denies} denies"),
            EvaluationScore("code_quality", code_quality, f"{n_errors} errors observed"),
            EvaluationScore("security", security, f"{denies} permission denials"),
            EvaluationScore("performance", performance, "heuristic baseline"),
        ]
        findings = []
        if n_errors:
            findings.append(f"{n_errors} tool errors recorded in audit log")
        if denies:
            findings.append(f"{denies} permission denial(s)")
        if "git_diff_stat" in evidence:
            findings.append("git diff present — code changes applied")
        suggestions = []
        if n_errors > 3:
            suggestions.append("Investigate root cause of recurring tool errors")
        if denies > 0:
            suggestions.append("Review permission denials — adjust permissions or approach")
        return scores, findings, suggestions

    # ── Write report ─────────────────────────────────────────────

    @staticmethod
    def write_report(report: EvaluationReport, workspace: Path) -> tuple[Path, Path]:
        """Write both SCORE.md and .score.json to the workspace."""
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        md_path = workspace / "SCORE.md"
        json_path = workspace / ".score.json"
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        json_path.write_text(report.to_json(), encoding="utf-8")
        return md_path, json_path


# ── Helpers ───────────────────────────────────────────────────────


def _count(items) -> dict:
    out: dict = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _parse_score_response(text: str) -> dict:
    """Tolerant JSON parser — handles markdown fences, smart quotes, trailing commas.

    PR-16: delegates to LLMExtractor._safe_json_loads (the canonical
    tolerant parser shared by all agent/agents/ JSON parsing sites).
    Returns {} on failure (caller treats {} as "no scores" / ABSTAIN).
    """
    from ..core.llm_extractor import LLMExtractor

    result = LLMExtractor._safe_json_loads(text)
    return result if isinstance(result, dict) else {}
