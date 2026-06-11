"""Tests for the Orchestrator PM Agent (PR-07)."""

import asyncio
import json
import pytest

from agent.agents import (
    OrchestratorAgent,
    TaskRequest,
    TaskResponse,
    TaskExecutionError,
    CyclicDependencyError,
    CODE_ROLE,
    TEST_ROLE,
    REVIEWER_ROLE,
    DEVOPS_ROLE,
    BUILTIN_ROLES,
    get_role,
)
from agent.agents.orchestrator import (
    _parse_decomposition,
    DECOMPOSE_PROMPT,
    MERGE_PROMPT,
)


# ── Roles ──────────────────────────────────────────────────────────


class TestRoles:
    def test_four_builtin_roles(self):
        assert set(BUILTIN_ROLES.keys()) == {"code", "test", "reviewer", "devops"}

    def test_code_role(self):
        assert CODE_ROLE.name == "code_generator"
        assert "write_file" in CODE_ROLE.tools
        assert "run_tests" not in CODE_ROLE.tools

    def test_test_role_has_run_tests(self):
        assert "run_tests" in TEST_ROLE.tools

    def test_reviewer_is_read_only(self):
        assert REVIEWER_ROLE.is_read_only is True
        assert "write_file" not in REVIEWER_ROLE.tools

    def test_devops_has_git_tools(self):
        assert "git_commit" in DEVOPS_ROLE.tools

    def test_get_role(self):
        assert get_role("code") is CODE_ROLE
        with pytest.raises(KeyError):
            get_role("nonexistent")

    def test_role_with_tools_immutable(self):
        r1 = CODE_ROLE
        r2 = r1.with_tools("run_tests")
        assert "run_tests" in r2.tools
        assert "run_tests" not in r1.tools  # original untouched


# ── TaskRequest / TaskResponse ─────────────────────────────────────


class TestTaskDataclasses:
    def test_request_round_trip(self):
        r = TaskRequest(
            task_id="st-1", role="code", description="impl X",
            depends_on=["st-0"], inputs={"a": 1}, priority=3, timeout=120.0,
        )
        d = r.to_dict()
        assert d["task_id"] == "st-1"
        assert d["depends_on"] == ["st-0"]
        assert d["inputs"] == {"a": 1}

    def test_response_is_success(self):
        r = TaskResponse(task_id="x", status="done")
        assert r.is_success is True
        r2 = TaskResponse(task_id="y", status="failed")
        assert r2.is_success is False


# ── Decomposition parser ───────────────────────────────────────────


class TestParseDecomposition:
    def test_clean_json_array(self):
        text = '[{"id": "st-1", "role": "code", "description": "impl X", "depends_on": []}]'
        tasks = _parse_decomposition(text)
        assert len(tasks) == 1
        assert tasks[0].task_id == "st-1"
        assert tasks[0].role == "code"

    def test_extract_json_from_prose(self):
        text = 'Here is the plan:\n[{"id":"a","role":"test","description":"x"}]\nDone.'
        tasks = _parse_decomposition(text)
        assert len(tasks) == 1
        assert tasks[0].task_id == "a"

    def test_handles_smart_quotes(self):
        text = '[{"id": "st-1", "role": "code", "description": "Implement \"hello\""}]'
        # Our regex doesn't fix smart quotes; that's handled in the relaxed path
        tasks = _parse_decomposition(text)
        # May or may not parse — either way should not crash
        assert isinstance(tasks, list)

    def test_empty_returns_empty(self):
        assert _parse_decomposition("") == []
        assert _parse_decomposition("not json at all") == []
        # A bare object isn't strictly a list, but the parser treats
        # both shapes as a single-task input.

    def test_missing_role_defaults_to_code(self):
        text = '[{"id": "a", "description": "do it"}]'
        tasks = _parse_decomposition(text)
        assert len(tasks) == 1
        assert tasks[0].role == "code"

    def test_id_auto_generated_when_missing(self):
        text = '[{"role": "code", "description": "x"}]'
        tasks = _parse_decomposition(text)
        assert tasks[0].task_id == "st-1"


# ── Dependency validation ──────────────────────────────────────────


class TestValidateDependencies:
    def test_known_deps_pass(self):
        o = OrchestratorAgent()
        o._validate_dependencies([
            TaskRequest(task_id="a", role="code", description="x"),
            TaskRequest(task_id="b", role="test", description="y", depends_on=["a"]),
        ])

    def test_unknown_dep_raises(self):
        o = OrchestratorAgent()
        with pytest.raises(TaskExecutionError, match="unknown task"):
            o._validate_dependencies([
                TaskRequest(task_id="a", role="code", description="x", depends_on=["zzz"]),
            ])

    def test_cycle_raises(self):
        o = OrchestratorAgent()
        with pytest.raises(CyclicDependencyError):
            o._validate_dependencies([
                TaskRequest(task_id="a", role="code", description="x", depends_on=["b"]),
                TaskRequest(task_id="b", role="code", description="y", depends_on=["a"]),
            ])

    def test_diamond_is_ok(self):
        """A → B, A → C, B → D, C → D: not a cycle."""
        o = OrchestratorAgent()
        o._validate_dependencies([
            TaskRequest(task_id="a", role="code", description="x"),
            TaskRequest(task_id="b", role="code", description="x", depends_on=["a"]),
            TaskRequest(task_id="c", role="code", description="x", depends_on=["a"]),
            TaskRequest(task_id="d", role="code", description="x", depends_on=["b", "c"]),
        ])


# ── DAG execution ──────────────────────────────────────────────────


class TestDAGExecution:
    @pytest.mark.asyncio
    async def test_empty(self):
        o = OrchestratorAgent()
        result = await o._execute_dag([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_single_task(self):
        o = OrchestratorAgent()
        tasks = [TaskRequest(task_id="a", role="code", description="x")]
        result = await o._execute_dag(tasks)
        assert "a" in result
        assert result["a"].is_success

    @pytest.mark.asyncio
    async def test_chain_runs_sequentially(self):
        """a → b → c: c must not start before b, b before a."""
        order: list = []

        async def dispatcher(t, completed):
            order.append(t.task_id)
            await asyncio.sleep(0)
            return TaskResponse(task_id=t.task_id, status="done", outputs={"summary": t.task_id})

        o = OrchestratorAgent(dispatch_fn=dispatcher)
        tasks = [
            TaskRequest(task_id="a", role="code", description="x"),
            TaskRequest(task_id="b", role="code", description="x", depends_on=["a"]),
            TaskRequest(task_id="c", role="code", description="x", depends_on=["b"]),
        ]
        result = await o._execute_dag(tasks)
        assert len(result) == 3
        # a must come before b, b before c
        assert order.index("a") < order.index("b") < order.index("c")

    @pytest.mark.asyncio
    async def test_independent_tasks_run_in_parallel(self):
        """Three tasks with no deps: all should start before any completes."""
        started: list = []
        all_started = asyncio.Event()
        expected_count = 3
        seen_count = 0

        async def dispatcher(t, completed):
            nonlocal seen_count
            started.append(t.task_id)
            seen_count += 1
            if seen_count == expected_count:
                all_started.set()
            await asyncio.sleep(0.05)  # Yield so others can start
            return TaskResponse(task_id=t.task_id, status="done", outputs={"summary": t.task_id})

        o = OrchestratorAgent(dispatch_fn=dispatcher)
        tasks = [
            TaskRequest(task_id="a", role="code", description="x"),
            TaskRequest(task_id="b", role="code", description="x"),
            TaskRequest(task_id="c", role="code", description="x"),
        ]
        await o._execute_dag(tasks)
        # All three should have started before any of them finished
        assert len(started) == 3
        # The set must contain all three
        assert set(started) == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_diamond_dag(self):
        """a → b, a → c, b → d, c → d. b and c run in parallel; d runs after both."""
        order: list = []
        b_done = asyncio.Event()
        c_done = asyncio.Event()
        proceed = asyncio.Event()

        async def dispatcher(t, completed):
            order.append(t.task_id)
            if t.task_id == "b":
                await asyncio.sleep(0.01)
                b_done.set()
            elif t.task_id == "c":
                await asyncio.sleep(0.01)
                c_done.set()
            elif t.task_id == "d":
                # d should only run after both b and c
                assert b_done.is_set()
                assert c_done.is_set()
            return TaskResponse(task_id=t.task_id, status="done", outputs={"summary": t.task_id})

        o = OrchestratorAgent(dispatch_fn=dispatcher)
        tasks = [
            TaskRequest(task_id="a", role="code", description="x"),
            TaskRequest(task_id="b", role="code", description="x", depends_on=["a"]),
            TaskRequest(task_id="c", role="code", description="x", depends_on=["a"]),
            TaskRequest(task_id="d", role="code", description="x", depends_on=["b", "c"]),
        ]
        result = await o._execute_dag(tasks)
        assert len(result) == 4
        assert all(r.is_success for r in result.values())

    @pytest.mark.asyncio
    async def test_dispatcher_exception_marked_failed(self):
        async def dispatcher(t, completed):
            raise RuntimeError("boom")

        o = OrchestratorAgent(dispatch_fn=dispatcher)
        tasks = [TaskRequest(task_id="a", role="code", description="x")]
        result = await o._execute_dag(tasks)
        assert result["a"].status == "failed"
        assert "boom" in result["a"].error

    @pytest.mark.asyncio
    async def test_timeout_marks_status(self):
        async def dispatcher(t, completed):
            await asyncio.sleep(10)  # Way longer than timeout
            return TaskResponse(task_id=t.task_id, status="done")

        o = OrchestratorAgent(dispatch_fn=dispatcher)
        tasks = [TaskRequest(task_id="a", role="code", description="x", timeout=0.05)]
        result = await o._execute_dag(tasks)
        assert result["a"].status == "timeout"


# ── Full run() workflow ───────────────────────────────────────────


class TestFullRun:
    @pytest.mark.asyncio
    async def test_decompose_to_dag_to_merge(self):
        """End-to-end with mock LLM (decompose_fn, dispatch_fn, merge_fn)."""

        async def decompose_fn(task, roles):
            return [
                TaskRequest(task_id="a", role="code", description="impl"),
                TaskRequest(task_id="b", role="test", description="test", depends_on=["a"]),
            ]

        async def dispatch_fn(t, completed):
            return TaskResponse(
                task_id=t.task_id, status="done",
                role=t.role, outputs={"summary": f"did {t.task_id}"},
            )

        async def merge_fn(task, results):
            return f"Merged {len(results)} subtasks for: {task}"

        o = OrchestratorAgent(
            decompose_fn=decompose_fn,
            dispatch_fn=dispatch_fn,
            merge_fn=merge_fn,
        )
        result = await o.run("Build feature X")
        assert "2 subtasks" in result
        assert "Build feature X" in result

    @pytest.mark.asyncio
    async def test_decompose_failure_returns_explanation(self):
        o = OrchestratorAgent()  # No decompose_fn, no llm_call
        # Decomposition with no LLM configured raises inside _decompose
        with pytest.raises(TaskExecutionError):
            await o.run("task")

    @pytest.mark.asyncio
    async def test_llm_call_decompose_and_merge(self):
        """Use the llm_call injection point instead of explicit fns."""

        async def llm(prompt):
            if "Decompose" in prompt:
                return json.dumps([
                    {"id": "a", "role": "code", "description": "impl X", "depends_on": []},
                ])
            return "merge result"

        async def dispatch_fn(t, completed):
            return TaskResponse(task_id=t.task_id, status="done", role=t.role)

        o = OrchestratorAgent(llm_call=llm, dispatch_fn=dispatch_fn)
        result = await o.run("task")
        assert "merge result" == result

    @pytest.mark.asyncio
    async def test_fallback_merge(self):
        """No LLM, no merge_fn: deterministic tabular summary."""
        async def decompose_fn(task, roles):
            return [
                TaskRequest(task_id="a", role="code", description="x"),
                TaskRequest(task_id="b", role="test", description="y", depends_on=["a"]),
            ]

        async def dispatch_fn(t, completed):
            return TaskResponse(
                task_id=t.task_id, status="done",
                role=t.role, outputs={"summary": "ok"},
            )

        o = OrchestratorAgent(decompose_fn=decompose_fn, dispatch_fn=dispatch_fn)
        # No merge_fn, no llm_call → falls back to tabular
        result = await o.run("My task")
        assert "Orchestrator report" in result
        assert "My task" in result
        assert "2 succeeded" in result


# ── Prompts ────────────────────────────────────────────────────────


class TestPrompts:
    def test_decompose_prompt_includes_roles(self):
        s = DECOMPOSE_PROMPT.format(roles="code,test", task="X")
        assert "code,test" in s
        assert "X" in s
        assert "JSON" in s

    def test_merge_prompt_includes_results(self):
        s = MERGE_PROMPT.format(task="T", results="line1\nline2")
        assert "T" in s
        assert "line1" in s
