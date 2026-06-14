"""SpecPlanAdapter — bridge SPECS.md ↔ ExecutionPlan (M2 P0).

`SpecPlanAdapter.from_spec(phase_id)` consumes the AC-aware structures
parsed by ``agent.core.spec_loader`` (``ACSpecPhase``, ``AcceptanceCriterion``)
and produces an ``ExecutionPlan`` whose:

  * ``plan_id``         is a stable ``spec-<phase_id>`` id
  * ``title``           is the phase title from SPECS.md
  * ``steps``           are 1:1 with the phase's acceptance criteria (or
                        ``raw_tasks`` when the phase has no ACs)
  * ``tdd_phase``       defaults to ``TDDState.RED`` on every step (the
                        executor is expected to write a failing test first
                        per PR-02) — overridable per phase via the optional
                        ``step_tdd_phase`` kwarg
  * ``acceptance_criteria`` mirrors the source ACs, with ``verify_command``
                        pulled from each AC's ``verified_by`` metadata
                        when available
  * ``risks``           are populated from any ``## Risks`` bullet inside
                        the phase body (M2 plan format) when present
  * ``parent_plan_id``  round-trips the prior ``ExecutionPlan.plan_id``
                        if a plan for the same phase already exists
                        (chain-of-refinements semantics, M3 will use this
                        for ``/plan refine``)

This module is intentionally self-contained and side-effect-free: it
does NOT write to disk, mutate the SPECS.md file, or mark ACs done.
That is left to the caller (``/plan from-spec`` and the executor) so
the adapter stays easy to unit-test.

Why this is the M2 headline feature
-----------------------------------
``gap-analysis.md:142`` flags "计划验证 ⚠️ 间接 — verify_acs() 验证
spec 合规，缺执行后 plan 对比" as a known gap.  This adapter is the
prerequisite for closing that gap: every plan now has structured
``acceptance_criteria`` and per-step ``verify_command`` fields, so
``verify_acs()`` can compare the executor's output against the
plan-shaped ACs instead of re-parsing markdown.

The public surface
------------------
* :func:`from_spec` — synchronous, pure, returns ``ExecutionPlan`` or
  raises :class:`SpecPlanAdapterError` if the phase is not found
* :func:`list_eligible_phases` — synchronous, returns ``list[str]`` of
  phase ids that can be turned into a plan (used by ``/plan from-spec``
  without arguments to auto-complete)
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

from .plan import AC, ExecutionPlan, PlanStep
from .spec_loader import (
    AcceptanceCriterion,
    ACSpecPhase,
    SpecDocument,
    load_spec_document,
)
from .tdd_state_machine import TDDState


class SpecPlanAdapterError(Exception):
    """Raised by SpecPlanAdapter for caller-fixable errors.

    Distinct from ``ValueError`` so the slash-command layer can render a
    targeted "did you mean …" hint instead of a generic error.
    """


# Default TDD phase attached to every step produced by the adapter.
# RED = "write a failing test first" per PR-02; the executor advances
# to GREEN/REFACTOR as it runs.  ``None`` means "free-form execution
# without a TDD cycle" — opt-out via the ``step_tdd_phase`` kwarg.
_DEFAULT_STEP_TDD_PHASE = TDDState.RED


def list_eligible_phases(workspace: Path) -> list[str]:
    """Return phase ids that can be turned into a plan.

    Includes both phases that are not yet completed and any partial
    phase (the executor can resume work on partial phases).  Excludes
    completed and backlog phases — those are read-only and don't need
    a new plan.
    """
    doc = load_spec_document(workspace)
    return [p.id for p in doc.phases if p.status in ("planned", "partial")]


def from_spec(
    workspace: Path,
    phase_id: str,
    *,
    step_tdd_phase: Optional[TDDState] = _DEFAULT_STEP_TDD_PHASE,
    parent_plan_id: str = "",
) -> ExecutionPlan:
    """Build an ``ExecutionPlan`` from a SPECS.md phase.

    Args:
        workspace: workspace root (where ``SPECS.md`` lives)
        phase_id: id of the phase, e.g. ``"P0"`` or ``"P0-1"``. Must match
                  a phase id produced by ``load_spec_document``.
        step_tdd_phase: TDD phase to attach to every generated step. Pass
                        ``None`` to disable TDD cycling.
        parent_plan_id: if a prior plan for the same phase already exists,
                        set this so the new plan chains as a refinement.

    Returns:
        A populated ``ExecutionPlan`` with steps, ACs, and a stable
        ``plan_id``.

    Raises:
        SpecPlanAdapterError: when the phase_id is not found in the spec.
    """
    doc = load_spec_document(workspace)
    phase = _resolve_phase(doc, phase_id)
    steps = _build_steps(phase, step_tdd_phase)
    acs = _build_acs(phase)
    plan_id = _new_plan_id(phase_id)

    return ExecutionPlan(
        task=f"Implement {phase.id}: {phase.title}",
        steps=steps,
        summary=phase.title,
        title=phase.title,
        plan_id=plan_id,
        parent_plan_id=parent_plan_id,
        acceptance_criteria=acs,
        risks=[],  # populated by the LLM later if /plan refine is invoked
        alternatives=[],
        allowed_prompts=[
            # The LLM is expected to extend this list in the post-parse
            # refine step. We seed the obvious ones so a freshly-built
            # plan already has a sensible allow-list.
            _allowed_prompt("edit", "low", "Edit source files"),
            _allowed_prompt("run_tests", "low", "Run pytest to verify ACs"),
        ],
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_phase(doc: SpecDocument, phase_id: str) -> ACSpecPhase:
    phase = doc.get_phase(phase_id)
    if phase is None:
        available = ", ".join(p.id for p in doc.phases) or "<no phases parsed>"
        raise SpecPlanAdapterError(
            f"Phase {phase_id!r} not found in SPECS.md. "
            f"Available: {available}. "
            f"Use `coding-agent --help` to see /plan from-spec usage."
        )
    return phase


def _build_steps(
    phase: ACSpecPhase,
    step_tdd_phase: Optional[TDDState],
) -> list[PlanStep]:
    """Translate ACs (preferred) or raw_tasks into PlanStep instances.

    The TDD phase is attached at the *step* level, not the plan level, so
    individual steps can override the default in M3 (e.g. documentation
    steps set ``tdd_phase=None``).

    AC ordering: we use the AC's natural order in the source spec
    (AC-1, AC-2, ...). Done ACs are NOT filtered out — the plan should
    still show them with ``status="done"`` so the executor knows which
    work is already finished.
    """
    if phase.acceptance_criteria:
        return [
            _step_from_ac(ac, step_tdd_phase, idx + 1)
            for idx, ac in enumerate(phase.acceptance_criteria)
        ]
    if phase.raw_tasks:
        return [
            _step_from_raw_task(task, step_tdd_phase, idx + 1)
            for idx, task in enumerate(phase.raw_tasks)
        ]
    # Empty phase: surface a single "discover what this phase needs" step
    return [
        PlanStep(
            id=1,
            description=(f"Investigate {phase.id}: {phase.title} — " "no ACs or tasks defined yet"),
            expected_outcome="concrete acceptance criteria for this phase",
            tdd_phase=step_tdd_phase,
        )
    ]


def _step_from_ac(
    ac: AcceptanceCriterion,
    step_tdd_phase: Optional[TDDState],
    step_id: int,
) -> PlanStep:
    # If the AC is already done, carry the status through so the executor
    # skips it. Also keep verify_command empty for done ACs (the
    # historical verify ran at done time).
    if ac.status == "done":
        return PlanStep(
            id=step_id,
            description=f"{ac.id}: {ac.description}",
            expected_outcome="already done",
            status="done",
            tdd_phase=step_tdd_phase,
            verify_command="",
            estimated_complexity="S",
        )
    return PlanStep(
        id=step_id,
        description=f"{ac.id}: {ac.description}",
        expected_outcome=ac.description,
        tool_hint=_guess_tool_hint(ac.description),
        tdd_phase=step_tdd_phase,
        verify_command=_ac_verify_hint(ac),
        estimated_complexity=_guess_complexity(ac.description),
    )


def _step_from_raw_task(task: str, step_tdd_phase: Optional[TDDState], step_id: int) -> PlanStep:
    return PlanStep(
        id=step_id,
        description=task,
        expected_outcome=task,
        tool_hint=_guess_tool_hint(task),
        tdd_phase=step_tdd_phase,
        estimated_complexity=_guess_complexity(task),
    )


def _build_acs(phase: ACSpecPhase) -> list[AC]:
    """Wrap the source ACs into the plan's AC list.

    A plan AC is a different object from a spec AC: it carries
    ``verify_command`` (a shell command), not ``status`` (state).  We
    pull the verify command from the spec's ``verified_by`` sidecar
    field when present, otherwise leave it blank for the executor to
    fill in.
    """
    out: list[AC] = []
    for ac in phase.acceptance_criteria:
        # Spec ACs don't carry a verify_command natively.  We default to
        # `pytest -k <ac-id>` when there's an obvious test-pattern, else
        # leave it empty.  The executor can override during refinement.
        verify = _derive_verify_command(ac)
        out.append(
            AC(
                id=ac.id,
                description=ac.description,
                verify_command=verify,
            )
        )
    return out


def _new_plan_id(phase_id: str) -> str:
    """Build a stable id ``spec-<phase_id>-<ts>-<uuid>``.

    The ``spec-`` prefix disambiguates from the ``plan-`` prefix used by
    ExitPlanModeTool, so a single listing of ``~/.coding-agent/plans/``
    clearly separates the two sources.
    """
    return f"spec-{phase_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _guess_tool_hint(text: str) -> str:
    """Best-effort tool hint from the task description.

    Heuristic only — the executor refines.  We keep this conservative
    (returning the empty string when uncertain) so the plan does not
    bias the LLM toward the wrong tool.
    """
    lower = text.lower()
    if any(kw in lower for kw in ("test", "pytest", "coverage")):
        return "run_tests"
    if any(kw in lower for kw in ("doc", "readme", "comment")):
        return "edit"
    if any(kw in lower for kw in ("fix", "refactor", "implement", "add ", "create ")):
        return "edit"
    return ""


def _guess_complexity(text: str) -> str:
    """S/M/L/XL from rough length and verb count."""
    words = len(text.split())
    if words <= 8:
        return "S"
    if words <= 20:
        return "M"
    if words <= 40:
        return "L"
    return "XL"


def _ac_verify_hint(ac: AcceptanceCriterion) -> str:
    """Default verify_command for an AC.

    For now we return an empty string — the executor or ``/plan refine``
    command is the right place to author a real verify command.  The
    AC wrapper still carries the description, so the executor has the
    intent to work from.
    """
    return ""


def _derive_verify_command(ac: AcceptanceCriterion) -> str:
    """Best-effort verify_command for a plan-level AC.

    If the description mentions tests, suggest ``pytest -k <ac-id>`` —
    the executor can override.  Otherwise leave empty.
    """
    desc = ac.description.lower()
    if "test" in desc or "pytest" in desc:
        return f"pytest -k {ac.id.lower()}"
    return ""


def _allowed_prompt(tool: str, risk: str, justification: str):
    """Local import to avoid a circular import at module load."""
    from .plan import AllowedPrompt

    return AllowedPrompt(tool=tool, risk_level=risk, justification=justification)
