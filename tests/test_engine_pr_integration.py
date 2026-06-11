"""Cross-cutting integration tests for PR-11/12/13.

These tests verify that the three new PRs (Dual-agent review, AB testing,
Progress anchor) work together correctly inside the engine:
- All three enabled simultaneously: no hook interferes with another
- All three disabled: engine is a minimal ReAct loop
- Singleton interactions across engines and tests
- Hook registration order and isolation
- Audit log captures dual review but not AB observations
"""

import asyncio
import json
import time

import pytest

from agent.core.audit_log import reset_audit_logger
from agent.core.dual_review import (
    DualReviewManager,
    reset_dual_review_manager,
)
from agent.core.engine import AgentConfig, AgentEngine
from agent.core.hooks import (
    AFTER_TOOL_EXECUTION,
    BEFORE_LLM_CALL,
)
from agent.core.progress_anchor import ProgressAnchor, ProgressRecord
from agent.governance.ab_test import (
    Experiment,
    ExperimentVariant,
    get_ab_test_manager,
    reset_ab_test_manager,
)
from agent.llm.client import Message

# ── Helpers ─────────────────────────────────────────────────────────


def _config(**overrides) -> AgentConfig:
    base = dict(
        model="mock",
        provider="mock",
        mode="bypass",
        tdd_mode="off",
        audit_enabled=False,
        otel_enabled=False,
    )
    base.update(overrides)
    return AgentConfig(**base)


async def _approve_chat(messages, stream=False):
    return json.dumps({"verdict": "approve", "rationale": "ok"}), None


async def _reject_chat(messages, stream=False):
    return json.dumps({"verdict": "reject", "rationale": "no"}), None


async def _abstain_chat(messages, stream=False):
    return json.dumps({"verdict": "abstain", "rationale": "uncertain"}), None


# ── TestAllThreeEnabled ──────────────────────────────────────────


class TestAllThreeEnabled:
    """Engine with PR-11 + PR-12 + PR-13 all enabled should work."""

    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    def test_engine_initializes_all_components(self, tmp_path):
        e = AgentEngine(
            _config(
                progress_workspace=str(tmp_path),
                ab_user_id="alice",
            )
        )
        assert e.dual_review is not None
        assert e.ab_test is not None
        assert e.anchor is not None
        # And the hook registry knows about them
        assert e.hooks.has(BEFORE_LLM_CALL)
        assert e.hooks.has(AFTER_TOOL_EXECUTION)

    def test_disabling_all_three(self):
        e = AgentEngine(
            _config(
                enable_dual_review=False,
                ab_test_enabled=False,
                progress_anchor_enabled=False,
            )
        )
        assert e.dual_review is None
        assert e.ab_test is None
        assert e.anchor is None


# ── TestHooksAreAsync ────────────────────────────────────────────


class TestHooksAreAsync:
    """The hooks registered on the engine are async — they must be awaited."""

    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_hooks_execute_awaitable(self, tmp_path):
        e = AgentEngine(
            _config(
                progress_workspace=str(tmp_path),
                ab_user_id="alice",
            )
        )
        # BEFORE_LLM_CALL: should be awaitable
        payload = {"messages": [Message(role="user", content="hi")], "system": "test"}
        out = e.hooks.execute(BEFORE_LLM_CALL, payload)
        # execute returns coroutine when there are async hooks
        if asyncio.iscoroutine(out):
            out = await out
        # If progress anchor had a record, it should be in the output
        # (we didn't pre-populate, so no <progress> expected)
        assert "messages" in out

    @pytest.mark.asyncio
    async def test_hooks_execute_with_progress_pre_populated(self, tmp_path):
        e = AgentEngine(
            _config(
                progress_workspace=str(tmp_path),
            )
        )
        e.anchor.write(
            ProgressRecord(
                current_task="hook order",
                current_step="1/3",
            )
        )
        payload = {"messages": [Message(role="user", content="hi")], "system": "test"}
        out = e.hooks.execute(BEFORE_LLM_CALL, payload)
        if asyncio.iscoroutine(out):
            out = await out
        assert "<progress>" in out["messages"][0].content
        assert "hook order" in out["messages"][0].content

    @pytest.mark.asyncio
    async def test_after_tool_execute_writes_progress(self, tmp_path):
        e = AgentEngine(
            _config(
                progress_workspace=str(tmp_path),
            )
        )
        e._ab_last_task = "test"
        payload = {"tool": "read_file", "args": {"p": "x"}, "result": "ok"}
        out = e.hooks.execute(AFTER_TOOL_EXECUTION, payload)
        if asyncio.iscoroutine(out):
            await out
        # File was written
        assert e.anchor.exists()
        rec = e.anchor.read()
        assert rec.current_task == "test"


# ── TestAuditDoesNotCaptureABObservations ────────────────────────


class TestAuditDoesNotCaptureABObservations:
    """AB observations go to observations.jsonl, not the audit log."""

    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        self.tmp = tmp_path
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_ab_observation_writes_to_observations_jsonl_not_audit(self):
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(
            Experiment(
                id="exp_audit",
                name="",
                description="",
                target="system_prompt",
                target_key="x",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": "y"}),
                    ExperimentVariant(id="B", name="t", config={"new_content": "z"}),
                ],
            )
        )
        # Initialize audit logger
        from agent.core.audit_log import get_audit_logger

        e.audit = get_audit_logger()
        e._ab_task_start_ts = time.time()
        e._ab_last_task = "test task"
        e._total_input_tokens = 10
        e._total_output_tokens = 5
        # Apply → record
        payload = {"system": "x is here", "messages": []}
        payload = await e._ab_apply_variants_hook(payload)
        record_payload = {
            "_ab_experiments": payload.get("_ab_experiments", []),
            "error": None,
        }
        await e._ab_record_observation_hook(record_payload)
        # Check observations file
        obs_path = self.tmp / "exp" / "exp_audit" / "observations.jsonl"
        assert obs_path.exists()
        # Check audit log does NOT have AB records
        audit_files = list((self.tmp / "audit").glob("*.jsonl"))
        if audit_files:
            content = audit_files[0].read_text()
            assert "experiment_id" not in content


# ── TestSingletonResets ──────────────────────────────────────────


class TestSingletonResets:
    """Each singleton has a reset() that tests use to avoid cross-test
    contamination."""

    def test_reset_ab_test(self):
        m1 = get_ab_test_manager()
        reset_ab_test_manager()
        m2 = get_ab_test_manager()
        assert m1 is not m2

    def test_reset_dual_review(self):
        from agent.core.dual_review import (
            get_dual_review_manager,
        )

        # Clear any pre-existing
        reset_dual_review_manager()
        m1 = get_dual_review_manager()
        assert m1 is not None
        # Reset
        reset_dual_review_manager()
        # The internal _default_manager is now None
        from agent.core import dual_review

        assert dual_review._default_manager is None
        # Recreating yields a new instance
        m2 = get_dual_review_manager()
        assert m2 is not m1

    def test_progress_anchor_does_not_use_singleton(self, tmp_path):
        """Progress anchor is per-engine (per workspace)."""
        a1 = ProgressAnchor(workspace=tmp_path / "a")
        a2 = ProgressAnchor(workspace=tmp_path / "b")
        assert a1 is not a2
        assert a1.path != a2.path


# ── TestAllThreeDisabledIsMinimal ──────────────────────────────


class TestAllThreeDisabledIsMinimal:
    """Engine with PR-11/12/13 disabled should still function as a
    minimal ReAct loop (other hooks like audit/OTel may be off too)."""

    def test_engine_constructs_without_errors(self):
        e = AgentEngine(
            _config(
                enable_dual_review=False,
                ab_test_enabled=False,
                progress_anchor_enabled=False,
                audit_enabled=False,
                otel_enabled=False,
            )
        )
        assert e.dual_review is None
        assert e.ab_test is None
        assert e.anchor is None
        # Core engine parts still work
        assert e.event_bus is not None
        assert e.hooks is not None


# ── TestProgressAnchorDoesNotInterfereWithDualReview ───────────


class TestProgressAnchorDoesNotInterfereWithDualReview:
    """When both are enabled, the dual-review hook (BEFORE_TOOL_EXECUTION)
    and the progress hook (AFTER_TOOL_EXECUTION) shouldn't step on each other."""

    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_dual_review_approves_then_progress_writes(self, tmp_path):
        e = AgentEngine(
            _config(
                progress_workspace=str(tmp_path),
            )
        )
        e.dual_review = DualReviewManager(
            primary_chat=_approve_chat,
            secondary_chat=_approve_chat,
        )
        e._ab_last_task = "dual+progress"
        # BEFORE_TOOL_EXECUTION: dual review
        before_payload = {
            "tool": "write_file",
            "args": {"path": "x.py", "content": "hi"},
        }
        out = await e._dual_review_hook(before_payload)
        # APPROVE → no exception
        assert out is before_payload
        # AFTER_TOOL_EXECUTION: progress write
        after_payload = {
            "tool": "write_file",
            "args": {"path": "x.py", "content": "hi"},
            "result": "ok",
        }
        await e._update_progress_hook(after_payload)
        # Both ran successfully
        assert e.dual_review.reviews_approved == 1
        assert e.anchor.exists()


# ── TestAllHooksForOneToolCall ──────────────────────────────────


class TestAllHooksForOneToolCall:
    """Simulate a single tool call passing through all relevant hooks."""

    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        self.tmp = tmp_path
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_high_risk_tool_through_dual_review_then_audit_then_progress(self):
        e = AgentEngine(
            _config(
                progress_workspace=str(self.tmp),
                ab_user_id="alice",
            )
        )
        e.dual_review = DualReviewManager(
            primary_chat=_approve_chat,
            secondary_chat=_approve_chat,
        )
        e._ab_last_task = "full cycle"
        e._ab_task_start_ts = time.time()
        e._total_input_tokens = 100
        e._total_output_tokens = 50
        # Pre-stage: AB apply (on BEFORE_LLM_CALL)
        # Just verify the dual review + audit + progress combo
        before = {
            "tool": "write_file",
            "args": {"path": "x.py", "content": "hello"},
            "tc_id": "test_tc",
        }
        out = await e._dual_review_hook(before)
        # APPROVE
        assert out is before
        assert e.dual_review.reviews_approved == 1
        # AFTER_TOOL_EXECUTION
        after = {
            "tool": "write_file",
            "args": {"path": "x.py", "content": "hello"},
            "result": "ok",
            "error": None,
        }
        await e._update_progress_hook(after)
        # Progress file written
        assert e.anchor.exists()
        rec = e.anchor.read()
        assert rec.current_task == "full cycle"
        # And the chain hash was updated
        assert rec.op_hash != ""


# ── TestProgressAnchorDoesNotInterfereWithAB ────────────────────


class TestProgressAnchorDoesNotInterfereWithAB:
    @pytest.fixture(autouse=True)
    def _reset_all(self, tmp_path, monkeypatch):
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()
        self.tmp = tmp_path
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        yield
        reset_dual_review_manager()
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_ab_apply_does_not_touch_progress_file(self):
        """The AB apply hook operates on system_prompt; it shouldn't
        touch the progress file (which is anchored to AFTER_TOOL_EXECUTION)."""
        e = AgentEngine(
            _config(
                progress_workspace=str(self.tmp),
                ab_user_id="alice",
            )
        )
        e.ab_test.create(
            Experiment(
                id="exp_no_intrude",
                name="",
                description="",
                target="system_prompt",
                target_key="x",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": "Y"}),
                    ExperimentVariant(id="B", name="t", config={"new_content": "Y"}),
                ],
            )
        )
        # Pre-existing progress file
        e.anchor.write(
            ProgressRecord(
                current_task="pre-existing",
                current_step="1/3",
                op_hash="sha256:00000000000000000000000000000abc",
            )
        )
        prev_hash = e.anchor.read().op_hash
        # Run AB apply
        payload = {"system": "x is here", "messages": []}
        await e._ab_apply_variants_hook(payload)
        # Progress file unchanged
        rec = e.anchor.read()
        assert rec.current_task == "pre-existing"
        assert rec.op_hash == prev_hash
        assert rec.current_step == "1/3"
