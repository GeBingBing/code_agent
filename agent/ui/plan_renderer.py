"""PlanRenderer — pluggable plan display (M3 P0).

This module abstracts how an ``ExecutionPlan`` is rendered to a user.
We define a :class:`PlanRenderer` Protocol and ship two implementations:

* :class:`RichPlanRenderer` — uses the Rich library to produce a
  colorised, foldable Tree + Table + Panel.  Used by the CLI and any
  surface that can consume Rich renderables.
* :class:`PlainTextRenderer` — a no-dependency fallback for the TUI
  and tests.  Returns plain strings (no ANSI codes).

Two more backends (Editor, Web) are sketched as :class:`EditorPlanRenderer`
and :class:`WebPlanRenderer` stubs.  M3 P0 ships them as
``NotImplementedError`` — they're placeholders so the call sites
compile.  M3 P1 will fill in the TUI's editor-in-$EDITOR hookup and a
``http://localhost:PORT/plans/<id>`` static-page renderer.

Why a renderer abstraction?
  * The CLI, TUI, editor hookup and a future web view all want
    different shapes (Tree, Markdown, $EDITOR, HTML).
  * ``ExecutionPlan.to_markdown()`` is still the file-serialization path
    (M1 / M2 contract).  This module is the terminal / UI path.
  * M3's ``/plan refine`` needs ``render_diff(old, new)`` to show the
    user what changed between refinements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, runtime_checkable

from ..core.plan import ExecutionPlan, PlanStep

# ── Public Protocol ──────────────────────────────────────────────────────


@runtime_checkable
class PlanRenderer(Protocol):
    """Render an ExecutionPlan to a user surface.

    All methods return a "Renderable" — any object the surface knows
    how to display.  For the CLI/TUI that's a Rich renderable (Panel,
    Table, Tree, Group, str).  For tests we use plain strings.
    """

    def render_full(self, plan: ExecutionPlan) -> object:
        """Render every section of the plan (steps, ACs, risks, mermaid)."""
        ...

    def render_diff(self, old: ExecutionPlan, new: ExecutionPlan) -> object:
        """Render a side-by-side / inline diff between two plan revisions.

        Used by ``/plan refine`` to show the user what changed.
        """
        ...

    def render_collapsed(self, plan: ExecutionPlan) -> object:
        """Render a one-screen summary — title, step count, AC count, score."""
        ...

    def render_step_foldable(self, plan: ExecutionPlan) -> List[object]:
        """Render each step as a foldable / expandable unit.

        The TUI uses this to support j/k navigation + Space to fold.
        Returns a list (one entry per step) so the caller can wrap
        each in a Foldable widget.
        """
        ...


# ── Plain-text renderer (always available) ───────────────────────────────


@dataclass
class _TextBox:
    """Minimal Renderable shim — just a string, printable as-is."""

    text: str

    def __str__(self) -> str:
        return self.text


class PlainTextRenderer:
    """No-dependency renderer for tests and the TUI fallback.

    The TUI uses RichPanelRenderer when Rich is available, this one
    otherwise.  Output is plain strings — no ANSI codes, no markup.
    """

    def render_full(self, plan: ExecutionPlan) -> object:
        return _TextBox(plan.to_markdown())

    def render_diff(self, old: ExecutionPlan, new: ExecutionPlan) -> object:
        lines = ["## Diff: old → new", ""]
        old_by_id = {s.id: s for s in old.steps}
        new_by_id = {s.id: s for s in new.steps}
        all_ids = sorted(set(old_by_id) | set(new_by_id))
        for sid in all_ids:
            o, n = old_by_id.get(sid), new_by_id.get(sid)
            if o is None and n is not None:
                lines.append(f"+ Step {n.id}: {n.description}")
            elif o is not None and n is None:
                lines.append(f"- Step {o.id}: {o.description}")
            elif o is not None and n is not None and o.description != n.description:
                lines.append(f"~ Step {sid}: {o.description!r} → {n.description!r}")
            elif o is not None and n is not None and o.status != n.status:
                lines.append(f"~ Step {sid}: status {o.status} → {n.status}")
        if len(lines) == 2:  # only the header
            lines.append("(no step changes)")
        # AC count delta
        ac_delta = len(new.acceptance_criteria) - len(old.acceptance_criteria)
        if ac_delta:
            lines.append(
                f"AC count: {len(old.acceptance_criteria)} → {len(new.acceptance_criteria)}"
            )
        return _TextBox("\n".join(lines))

    def render_collapsed(self, plan: ExecutionPlan) -> object:
        pending = sum(1 for s in plan.steps if s.status != "done")
        score = getattr(plan, "review_notes", "")  # cheap reuse, no recompute
        return _TextBox(
            f"[{plan.title or plan.summary or plan.task}] "
            f"{len(plan.steps)} steps ({pending} pending), "
            f"{len(plan.acceptance_criteria)} ACs, "
            f"revision {plan.revision}"
        )

    def render_step_foldable(self, plan: ExecutionPlan) -> List[object]:
        return [_TextBox(f"Step {s.id}: {s.description}") for s in plan.steps]


# ── Rich renderer (CLI / any Rich-capable surface) ───────────────────────


class RichPlanRenderer:
    """Colorised renderer backed by the Rich library.

    Output: ``rich.tree.Tree`` for full plan, ``rich.table.Table`` for
    collapsed summary, ``rich.console.Group`` for diffs.  All methods
    return Rich renderables; the caller wraps them in a Console and
    prints / saves as needed.
    """

    def __init__(self, use_color: bool = True):
        self.use_color = use_color

    def _build(self) -> tuple:
        """Lazy-import rich so this module stays import-safe without rich.

        Returns (Tree, Table, Panel, Group) — the four classes we use.
        """
        try:
            from rich.console import Group
            from rich.panel import Panel
            from rich.table import Table
            from rich.tree import Tree
        except ImportError as e:  # pragma: no cover — rich is a dev dep
            raise RuntimeError(
                "RichPlanRenderer requires the `rich` package; " "install with `pip install rich`."
            ) from e
        return Tree, Table, Panel, Group

    def _icon(self, status: str) -> str:
        return {
            "pending": "○",
            "in_progress": "◉",
            "done": "✓",
            "skipped": "−",
            "failed": "✗",
        }.get(status, "?")

    def render_full(self, plan: ExecutionPlan) -> object:
        Tree, _Table, Panel, Group = self._build()
        title = plan.title or plan.summary or plan.task[:60]
        root = Tree(f"[bold cyan]{title}[/] r{plan.revision}")
        if plan.summary and plan.summary != title:
            root.add(f"[dim]{plan.summary}[/]")

        # Steps
        if plan.steps:
            step_node = root.add("[bold]Steps[/]")
            for s in plan.steps:
                tag_bits = []
                if s.tdd_phase:
                    tag_bits.append(f"tdd:{s.tdd_phase.value}")
                if s.estimated_complexity:
                    tag_bits.append(f"size:{s.estimated_complexity}")
                tag = f" [dim]({', '.join(tag_bits)})[/]" if tag_bits else ""
                line = f"{self._icon(s.status)} {s.description}{tag}"
                if s.verify_command:
                    line += f"  [dim]verify: `{s.verify_command}`[/]"
                if s.dependencies:
                    deps = ", ".join(f"#{d}" for d in s.dependencies)
                    line += f"  [dim]deps: {deps}[/]"
                step_node.add(line)

        # ACs
        if plan.acceptance_criteria:
            ac_node = root.add("[bold]Acceptance Criteria[/]")
            for ac in plan.acceptance_criteria:
                line = f"{ac.id}: {ac.description}"
                if ac.verify_command:
                    line += f"  [dim]verify: `{ac.verify_command}`[/]"
                ac_node.add(line)

        # Risks
        if plan.risks:
            risk_node = root.add("[bold]Risks[/]")
            for r in plan.risks:
                risk_node.add(
                    f"[{ 'red' if r.severity in ('high', 'critical') else 'yellow' }]"
                    f"{r.category} ({r.severity})[/] — {r.mitigation or 'no mitigation'}"
                )

        # Allowed actions
        if plan.allowed_prompts:
            ap_node = root.add("[bold]Allowed Actions[/]")
            for ap in plan.allowed_prompts:
                ap_node.add(
                    f"{ap.tool} ({ap.risk_level} risk) — {ap.justification or 'no justification'}"
                )

        # Review notes
        if plan.review_notes:
            root.add(f"[dim]{plan.review_notes}[/]")

        return Panel(root, title=f"Plan {plan.plan_id or '(no id)'}", border_style="cyan")

    def render_diff(self, old: ExecutionPlan, new: ExecutionPlan) -> object:
        Tree, _Table, Panel, _Group = self._build()
        root = Tree(
            f"[bold]Diff[/]: r{old.revision} → r{new.revision}  "
            f"([cyan]+{len(new.steps) - len(old.steps)}[/] steps, "
            f"[cyan]+{len(new.acceptance_criteria) - len(old.acceptance_criteria)}[/] ACs)"
        )
        old_by_id = {s.id: s for s in old.steps}
        new_by_id = {s.id: s for s in new.steps}
        all_ids = sorted(set(old_by_id) | set(new_by_id))
        for sid in all_ids:
            o, n = old_by_id.get(sid), new_by_id.get(sid)
            if o is None and n is not None:
                root.add(f"[green]+ Step {n.id}: {n.description}[/]")
            elif o is not None and n is None:
                root.add(f"[red]- Step {o.id}: {o.description}[/]")
            elif o is not None and n is not None:
                if o.description != n.description:
                    root.add(
                        f"[yellow]~ Step {sid}[/]: "
                        f"[red]{o.description!r}[/] → [green]{n.description!r}[/]"
                    )
                if o.status != n.status:
                    root.add(
                        f"[yellow]~ Step {sid}[/]: status "
                        f"[red]{o.status}[/] → [green]{n.status}[/]"
                    )
        return Panel(root, title="Plan Refinement Diff", border_style="magenta")

    def render_collapsed(self, plan: ExecutionPlan) -> object:
        _, Table, Panel, _Group = self._build()
        pending = sum(1 for s in plan.steps if s.status != "done")
        done = sum(1 for s in plan.steps if s.status == "done")
        t = Table(show_header=False, box=None, padding=(0, 1))
        t.add_column(style="bold")
        t.add_column()
        t.add_row("Plan", plan.title or plan.summary or plan.task[:60])
        t.add_row(
            "Revision",
            f"r{plan.revision}"
            + (f" (parent: {plan.parent_plan_id})" if plan.parent_plan_id else ""),
        )
        t.add_row("Steps", f"{done} done / {pending} pending / {len(plan.steps)} total")
        t.add_row("ACs", str(len(plan.acceptance_criteria)))
        t.add_row("Risks", str(len(plan.risks)))
        t.add_row("Allowed actions", str(len(plan.allowed_prompts)))
        return Panel(t, title="Plan Summary", border_style="green")

    def render_step_foldable(self, plan: ExecutionPlan) -> List[object]:
        out: List[object] = []
        for s in plan.steps:
            out.append(self._render_one_step(s))
        return out

    def _render_one_step(self, s: PlanStep) -> object:
        Tree, _Table, Panel, _Group = self._build()
        title = f"Step {s.id}: {s.description}"
        body = []
        if s.tool_hint:
            body.append(f"Tool: `{s.tool_hint}`")
        if s.expected_outcome:
            body.append(f"Expect: {s.expected_outcome}")
        if s.dependencies:
            body.append(f"Deps: {', '.join(f'#{d}' for d in s.dependencies)}")
        if s.verify_command:
            body.append(f"Verify: `{s.verify_command}`")
        if s.estimated_complexity:
            body.append(f"Size: {s.estimated_complexity}")
        if s.tdd_phase:
            body.append(f"TDD: {s.tdd_phase.value}")
        return Panel(
            "\n".join(body) or "(no details)",
            title=f"{self._icon(s.status)} {title}",
            border_style="blue",
        )


# ── Placeholder backends (M3 P1) ─────────────────────────────────────────


class EditorPlanRenderer:
    """Stub — M3 P1 will hook this up to $EDITOR.

    For now, ``render_full`` raises to make the unimplemented path
    obvious at call sites.  The CLI / TUI must check feature flags
    before dispatching to this renderer.
    """

    def render_full(self, plan: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError(
            "EditorPlanRenderer is M3 P1; use RichPlanRenderer or PlainTextRenderer"
        )

    def render_diff(self, old: ExecutionPlan, new: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError("EditorPlanRenderer is M3 P1")

    def render_collapsed(self, plan: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError("EditorPlanRenderer is M3 P1")

    def render_step_foldable(self, plan: ExecutionPlan) -> List[object]:  # pragma: no cover
        raise NotImplementedError("EditorPlanRenderer is M3 P1")


class WebPlanRenderer:
    """Stub — M3 P1 will render plans to a local HTTP page."""

    def render_full(self, plan: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError("WebPlanRenderer is M3 P1")

    def render_diff(self, old: ExecutionPlan, new: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError("WebPlanRenderer is M3 P1")

    def render_collapsed(self, plan: ExecutionPlan) -> object:  # pragma: no cover
        raise NotImplementedError("WebPlanRenderer is M3 P1")

    def render_step_foldable(self, plan: ExecutionPlan) -> List[object]:  # pragma: no cover
        raise NotImplementedError("WebPlanRenderer is M3 P1")


# ── Convenience factory ──────────────────────────────────────────────────


def get_default_renderer() -> PlanRenderer:
    """Return RichPlanRenderer if rich is importable, else PlainTextRenderer.

    Used by ``/plan show`` and the CLI's plan panel.  The TUI has its
    own selection logic (it always wants Rich renderables).
    """
    try:
        import rich  # noqa: F401

        return RichPlanRenderer()
    except ImportError:
        return PlainTextRenderer()
