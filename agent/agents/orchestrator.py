"""Orchestrator PM Agent — decompose / schedule / merge (PR-07).

The orchestrator is the "Project Manager" role from docs/1.md §7.1. It:
1. Decomposes a complex task into 3-8 atomic subtasks (LLM-driven).
2. Builds a DAG from explicit `depends_on` relations.
3. Schedules subtasks: independent tasks run in parallel, dependent
   tasks wait for their prerequisites.
4. Each subtask is dispatched to a role-specialized sub-agent (via
   EventBus; the actual LLM call is left to the engine).
5. Results are merged into a final summary (LLM-driven).

Design notes:
- Decomposition and merge are LLM-driven, but the *plumbing* (DAG
  scheduling, timeout, parallel dispatch) is deterministic.
- For testing without a real LLM, the orchestrator accepts
  `decompose_fn` and `merge_fn` callables that can be replaced.
- The orchestrator does NOT block the parent agent. The engine's
  `run_with_orchestrator` awaits the final result.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .roles import AgentRole, BUILTIN_ROLES, get_role


# ── Errors ──────────────────────────────────────────────────────────


class TaskExecutionError(Exception):
    """Raised when a subtask fails to execute."""


class CyclicDependencyError(Exception):
    """Raised when the dependency graph contains a cycle."""


# ── Task protocol ──────────────────────────────────────────────────


@dataclass
class TaskRequest:
    """A single subtask in the orchestrator's DAG."""
    task_id: str
    role: str  # "code" | "test" | "reviewer" | "devops"
    description: str
    parent_task_id: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    priority: int = 5  # 1 (highest) — 10 (lowest)
    timeout: float = 600.0  # seconds

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "description": self.description,
            "parent_task_id": self.parent_task_id,
            "inputs": dict(self.inputs),
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "timeout": self.timeout,
        }


@dataclass
class TaskResponse:
    """Result of a subtask execution."""
    task_id: str
    status: str  # "done" | "failed" | "timeout"
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration: float = 0.0
    role: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "outputs": dict(self.outputs),
            "error": self.error,
            "duration": self.duration,
            "role": self.role,
            "description": self.description,
        }

    @property
    def is_success(self) -> bool:
        return self.status == "done"


# ── Decomposition (LLM-driven) ────────────────────────────────────


# Default decomposition prompt — kept as a module-level constant so it
# can be inspected and overridden in tests.
DECOMPOSE_PROMPT = """\
Decompose the following task into 3-8 atomic subtasks. Each subtask must be:
- Atomic: one logical step (e.g., "implement X", "write tests for Y").
- Assignable to a single role: {roles}
- Either independent (no deps) or with explicit dependencies on other subtasks.

Output STRICT JSON (a list of objects), no prose:
[
  {{
    "id": "st-1",
    "role": "code",
    "description": "Implement the X function in module Y",
    "depends_on": []
  }},
  {{
    "id": "st-2",
    "role": "test",
    "description": "Write unit tests for X",
    "depends_on": ["st-1"]
  }}
]

Task: {task}

Subtasks (JSON only):"""


MERGE_PROMPT = """\
Original task: {task}

Subtask results:
{results}

Synthesize a final report that:
1. Summarizes what was done in 1-3 sentences.
2. Notes any failures or open issues.
3. Provides concrete next steps.

Output plain text."""


def _parse_decomposition(text: str) -> List[TaskRequest]:
    """Parse an LLM response into TaskRequest list. Tolerant to noise.

    PR-16: delegates JSON parsing to LLMExtractor._safe_json_loads
    (the shared tolerant parser). Handles markdown fences, smart quotes,
    trailing commas, and prose-embedded JSON.
    """
    from ..core.llm_extractor import LLMExtractor
    data = LLMExtractor._safe_json_loads(text)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    tasks: List[TaskRequest] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tasks.append(TaskRequest(
            task_id=str(item.get("id") or f"st-{len(tasks) + 1}"),
            role=str(item.get("role") or "code"),
            description=str(item.get("description") or "").strip(),
            depends_on=list(item.get("depends_on") or []),
        ))
    return tasks


# ── Orchestrator ───────────────────────────────────────────────────


# Type aliases for injection points.
DecomposeFn = Callable[[str, List[AgentRole]], Awaitable[List[TaskRequest]]]
MergeFn = Callable[[str, Dict[str, TaskResponse]], Awaitable[str]]
DispatchFn = Callable[[TaskRequest, Dict[str, TaskResponse]], Awaitable[TaskResponse]]


class OrchestratorAgent:
    """PM: decompose → DAG schedule → dispatch → merge.

    The orchestrator is the *control plane*; it does not itself call the
    LLM to execute subtasks. It dispatches them via `dispatch_fn` (which
    the engine wires up to invoke a role-specialized sub-agent).

    Default behavior:
    - `dispatch_fn` is provided externally (in production: by the engine)
    - `decompose_fn`/`merge_fn` use the LLM through `llm_call`
    """

    def __init__(
        self,
        *,
        roles: Optional[Dict[str, AgentRole]] = None,
        decompose_fn: Optional[DecomposeFn] = None,
        merge_fn: Optional[MergeFn] = None,
        dispatch_fn: Optional[DispatchFn] = None,
        llm_call: Optional[Callable[[str], Awaitable[str]]] = None,
    ):
        self.roles = roles or BUILTIN_ROLES
        self._decompose_fn = decompose_fn
        self._merge_fn = merge_fn
        self._dispatch_fn = dispatch_fn
        self._llm_call = llm_call

    # ── Public API ────────────────────────────────────────────────

    async def run(self, task: str, parent_task_id: Optional[str] = None) -> str:
        """Run the full PM workflow: decompose → schedule → merge."""
        # Step 1: decompose
        sub_tasks = await self._decompose(task)
        if not sub_tasks:
            return (
                f"Orchestrator could not decompose task: {task!r}. "
                "Provide clearer input or override `_decompose_fn`."
            )
        # Step 2: validate deps
        self._validate_dependencies(sub_tasks)
        # Step 3: tag with parent
        for st in sub_tasks:
            st.parent_task_id = parent_task_id
        # Step 4: schedule
        results = await self._execute_dag(sub_tasks)
        # Step 5: merge
        return await self._merge(task, results)

    async def decompose_only(self, task: str) -> List[TaskRequest]:
        """Expose the decomposition step for inspection / testing."""
        return await self._decompose(task)

    async def execute_dag(self, tasks: List[TaskRequest]) -> Dict[str, TaskResponse]:
        """Expose the DAG scheduler for inspection / testing."""
        self._validate_dependencies(tasks)
        return await self._execute_dag(tasks)

    # ── Steps ─────────────────────────────────────────────────────

    async def _decompose(self, task: str) -> List[TaskRequest]:
        if self._decompose_fn is not None:
            return await self._decompose_fn(task, list(self.roles.values()))
        if self._llm_call is None:
            raise TaskExecutionError(
                "Orchestrator has neither `decompose_fn` nor `llm_call` configured."
            )
        prompt = DECOMPOSE_PROMPT.format(
            roles=", ".join(self.roles.keys()),
            task=task,
        )
        text = await self._llm_call(prompt)
        return _parse_decomposition(text)

    async def _merge(self, task: str, results: Dict[str, TaskResponse]) -> str:
        if self._merge_fn is not None:
            return await self._merge_fn(task, results)
        if self._llm_call is None:
            # Deterministic fallback: tabular summary
            return self._fallback_merge(task, results)
        results_text = "\n".join(
            f"[{tid}] role={r.role} status={r.status} duration={r.duration:.2f}s "
            f"summary={r.outputs.get('summary', '')!r}"
            for tid, r in sorted(results.items())
        )
        prompt = MERGE_PROMPT.format(task=task, results=results_text)
        return await self._llm_call(prompt)

    @staticmethod
    def _fallback_merge(task: str, results: Dict[str, TaskResponse]) -> str:
        lines = [f"Orchestrator report for: {task!r}", ""]
        succeeded = sum(1 for r in results.values() if r.is_success)
        failed = sum(1 for r in results.values() if not r.is_success)
        lines.append(f"Subtasks: {succeeded} succeeded, {failed} failed")
        for tid, r in sorted(results.items()):
            status = "✓" if r.is_success else "✗"
            summary = r.outputs.get("summary", r.error or "")
            lines.append(f"  {status} [{tid}] {r.role}: {summary}")
        return "\n".join(lines)

    # ── DAG scheduling ────────────────────────────────────────────

    def _validate_dependencies(self, tasks: List[TaskRequest]) -> None:
        """Reject cycles; reject deps pointing to unknown tasks."""
        ids = {t.task_id for t in tasks}
        for t in tasks:
            for dep in t.depends_on:
                if dep not in ids:
                    raise TaskExecutionError(
                        f"Task {t.task_id!r} depends on unknown task {dep!r}"
                    )
        # Topological sort (Kahn's algorithm) to detect cycles
        in_deg: Dict[str, int] = {t.task_id: 0 for t in tasks}
        children: Dict[str, List[str]] = {t.task_id: [] for t in tasks}
        for t in tasks:
            for dep in t.depends_on:
                in_deg[t.task_id] += 1
                children[dep].append(t.task_id)
        ready = sorted([tid for tid, d in in_deg.items() if d == 0])
        visited = 0
        while ready:
            nxt = ready.pop(0)
            visited += 1
            for c in children[nxt]:
                in_deg[c] -= 1
                if in_deg[c] == 0:
                    ready.append(c)
        if visited != len(tasks):
            raise CyclicDependencyError(
                f"Cyclic dependency in task graph: {len(tasks) - visited} tasks unreachable"
            )

    async def _execute_dag(self, tasks: List[TaskRequest]) -> Dict[str, TaskResponse]:
        """DAG scheduler: parallel-ready, sequential-on-dep.

        Independent tasks (no remaining deps) run concurrently. As each
        task completes, its dependents become eligible.
        """
        by_id: Dict[str, TaskRequest] = {t.task_id: t for t in tasks}
        completed: Dict[str, TaskResponse] = {}
        in_flight: Dict[str, asyncio.Task] = {}

        async def _run_one(t: TaskRequest) -> TaskResponse:
            if self._dispatch_fn is None:
                # No dispatcher — synthesize a trivial "ok" response
                await asyncio.sleep(0)
                return TaskResponse(
                    task_id=t.task_id,
                    status="done",
                    role=t.role,
                    description=t.description,
                    outputs={"summary": f"Executed {t.task_id} (no dispatcher)"},
                )
            start = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    self._dispatch_fn(t, completed),
                    timeout=t.timeout,
                )
                resp.duration = time.monotonic() - start
                return resp
            except asyncio.TimeoutError:
                return TaskResponse(
                    task_id=t.task_id,
                    status="timeout",
                    role=t.role,
                    description=t.description,
                    error=f"timeout after {t.timeout}s",
                    duration=time.monotonic() - start,
                )
            except Exception as e:
                return TaskResponse(
                    task_id=t.task_id,
                    status="failed",
                    role=t.role,
                    description=t.description,
                    error=str(e),
                    duration=time.monotonic() - start,
                )

        while len(completed) < len(tasks):
            ready = [
                t for t in tasks
                if t.task_id not in completed
                and t.task_id not in in_flight
                and all(dep in completed for dep in t.depends_on)
            ]
            if not ready and not in_flight:
                # Shouldn't happen if validation passed, but guard against
                # races where dispatch errors out without recording.
                raise TaskExecutionError(
                    "DAG scheduler deadlock: no ready tasks and none in flight"
                )
            # Start all newly-ready tasks
            for t in ready:
                in_flight[t.task_id] = asyncio.create_task(_run_one(t))
            if not in_flight:
                break
            # Wait for any to complete
            done, _ = await asyncio.wait(
                in_flight.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for d in done:
                # Find the matching task_id
                tid = next(
                    (tid for tid, c in in_flight.items() if c is d),
                    None,
                )
                if tid is None:
                    continue
                try:
                    resp = d.result()
                except Exception as e:
                    resp = TaskResponse(
                        task_id=tid,
                        status="failed",
                        error=f"scheduler exception: {e}",
                    )
                completed[tid] = resp
                del in_flight[tid]
        return completed


# ── Helpers ───────────────────────────────────────────────────────


def make_task_id(prefix: str = "st") -> str:
    """Generate a short unique task id."""
    return f"{prefix}-{uuid.uuid4().hex[:6]}"
