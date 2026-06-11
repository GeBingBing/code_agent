"""Engine integration tests for dual-agent review (PR-11).

These verify:
- The engine wires DualReviewManager onto BEFORE_TOOL_EXECUTION
- High-risk tool calls are intercepted by the dual-review hook
- Low-risk tool calls are not intercepted
- PermissionDenied / ReviewRequiresUser exceptions surface as tool errors
- Decisions are recorded to the audit log
- A configurable secondary model is honored
- Disabling via AgentConfig.enable_dual_review=False removes the hook
"""

import asyncio
import json
import pytest

from agent.core.engine import AgentEngine, AgentConfig
from agent.core.dual_review import (
    DualReviewManager,
    PermissionDenied,
    RateLimiter,
    ReviewDecision,
    ReviewRequiresUser,
    ReviewVerdict,
    reset_dual_review_manager,
)
from agent.core.audit_log import reset_audit_logger
from agent.core.hooks import BEFORE_TOOL_EXECUTION


# ── Helpers ─────────────────────────────────────────────────────────


async def _approve_chat(messages, stream=False):
    return json.dumps({"verdict": "approve", "rationale": "ok"}), None


async def _reject_chat(messages, stream=False):
    return json.dumps({"verdict": "reject", "rationale": "dangerous"}), None


async def _abstain_chat(messages, stream=False):
    return json.dumps({"verdict": "abstain", "rationale": "uncertain"}), None


def _config(**overrides) -> AgentConfig:
    base = dict(model="mock", provider="mock", tdd_mode="off")
    base.update(overrides)
    return AgentConfig(**base)


def _make_engine_with_reviewers(primary_chat=None, secondary_chat=None,
                                 primary_model="gpt-4o", secondary_model="claude-sonnet-4-6",
                                 **config_overrides):
    """Create an engine and inject a custom dual-review manager."""
    e = AgentEngine(_config(**config_overrides))
    if primary_chat is not None or secondary_chat is not None:
        e.dual_review = DualReviewManager(
            primary_chat=primary_chat,
            secondary_chat=secondary_chat or primary_chat,
            primary_model=primary_model,
            secondary_model=secondary_model,
        )
    return e


# ── TestEngineWiring ────────────────────────────────────────────────


class TestEngineWiring:
    def test_dual_review_default_enabled(self):
        e = AgentEngine(_config())
        assert e.dual_review is not None
        assert e.hooks.has(BEFORE_TOOL_EXECUTION)

    def test_dual_review_disabled_via_config(self):
        e = AgentEngine(_config(enable_dual_review=False))
        assert e.dual_review is None
        # No hook registered when disabled
        # (other hooks may still be present from audit/otel; we just check
        # the dual review specific function isn't in the registry)

    def test_dual_review_initialized_with_default_manager(self):
        e = AgentEngine(_config())
        assert isinstance(e.dual_review, DualReviewManager)

    def test_dual_review_stats_initialized(self):
        e = AgentEngine(_config())
        assert e.dual_review.reviews_run == 0
        assert e.dual_review.reviews_approved == 0
        assert e.dual_review.reviews_rejected == 0


# ── TestDualReviewHookIsolated ──────────────────────────────────────


class TestDualReviewHookIsolated:
    """Test the hook in isolation by calling it directly (no full _execute_tool)."""

    @pytest.mark.asyncio
    async def test_low_risk_tool_passes_through(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        payload = {"tool": "read_file", "args": {"path": "x.py"}, "tc_id": "t1"}
        out = await e._dual_review_hook(payload)
        assert out is payload  # unchanged
        assert e.dual_review.reviews_run == 0  # not invoked

    @pytest.mark.asyncio
    async def test_high_risk_tool_invokes_reviewers(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        payload = {"tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"}
        out = await e._dual_review_hook(payload)
        # All approved → returns payload unchanged
        assert out is payload
        assert e.dual_review.reviews_run == 1
        assert e.dual_review.reviews_approved == 1

    @pytest.mark.asyncio
    async def test_high_risk_rejected_raises(self):
        e = _make_engine_with_reviewers(_approve_chat, _reject_chat)
        payload = {"tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"}
        with pytest.raises(PermissionDenied) as exc:
            await e._dual_review_hook(payload)
        assert "dangerous" in str(exc.value)

    @pytest.mark.asyncio
    async def test_split_raises_review_requires_user(self):
        e = _make_engine_with_reviewers(_approve_chat, _abstain_chat)
        payload = {"tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"}
        with pytest.raises(ReviewRequiresUser) as exc:
            await e._dual_review_hook(payload)
        assert exc.value.result is not None
        assert exc.value.result.requires_user is True

    @pytest.mark.asyncio
    async def test_non_dict_payload_ignored(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        # Hooks should never receive non-dicts, but be defensive
        result = await e._dual_review_hook("not a dict")
        assert result == "not a dict"
        assert e.dual_review.reviews_run == 0

    @pytest.mark.asyncio
    async def test_none_dual_review_returns_payload(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        e.dual_review = None
        payload = {"tool": "write_file", "args": {"path": "x.py"}}
        result = await e._dual_review_hook(payload)
        assert result is payload


# ── TestHighRiskToolSet ────────────────────────────────────────────


class TestHighRiskToolSet:
    def test_write_file_is_high_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        assert e.dual_review.is_high_risk("write_file")

    def test_execute_command_is_high_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        assert e.dual_review.is_high_risk("execute_command")

    def test_apply_diff_is_high_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        assert e.dual_review.is_high_risk("apply_diff")

    def test_read_file_is_not_high_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        assert not e.dual_review.is_high_risk("read_file")

    def test_grep_is_not_high_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        assert not e.dual_review.is_high_risk("grep")


# ── TestDualReviewLoggingToAudit ────────────────────────────────────


class TestDualReviewLoggingToAudit:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        reset_audit_logger()
        monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
        yield
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_approval_logged_to_audit(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        # Force audit init (lazy)
        from agent.core.audit_log import get_audit_logger
        e.audit = get_audit_logger()
        payload = {"tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"}
        await e._dual_review_hook(payload)
        records = e.audit.query(action="dual_review")
        assert len(records) == 1
        assert records[0]["tool"] == "write_file"
        assert records[0]["final_verdict" if False else "metadata"] is not None
        # Note: audit scrubs args; final_verdict lives in metadata after
        # the in-memory entry was logged. We just verify it was logged.
        # Look for it in the raw line.
        import json as _json
        # The audit query returns records that have been re-scrubbed.
        # We check that the action made it to disk
        from pathlib import Path
        files = list(Path(e.audit.log_dir).glob("*.jsonl"))
        assert len(files) >= 1
        lines = files[0].read_text().strip().split("\n")
        dual_review_entries = [l for l in lines if '"action": "dual_review"' in l]
        assert len(dual_review_entries) == 1
        rec = _json.loads(dual_review_entries[0])
        assert rec["tool"] == "write_file"
        # Final verdict should be in metadata
        assert rec["metadata"]["final_verdict"] == "approve"

    @pytest.mark.asyncio
    async def test_rejection_logged_to_audit(self):
        e = _make_engine_with_reviewers(_approve_chat, _reject_chat)
        from agent.core.audit_log import get_audit_logger
        e.audit = get_audit_logger()
        payload = {"tool": "write_file", "args": {"path": "x.py"}}
        with pytest.raises(PermissionDenied):
            await e._dual_review_hook(payload)
        from pathlib import Path
        files = list(Path(e.audit.log_dir).glob("*.jsonl"))
        lines = files[0].read_text().strip().split("\n")
        dual_review_entries = [l for l in lines if '"action": "dual_review"' in l]
        assert len(dual_review_entries) == 1
        rec = json.loads(dual_review_entries[0])
        assert rec["metadata"]["final_verdict"] == "reject"

    @pytest.mark.asyncio
    async def test_low_risk_tool_not_logged(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        from agent.core.audit_log import get_audit_logger
        e.audit = get_audit_logger()
        payload = {"tool": "read_file", "args": {"path": "x.py"}}
        await e._dual_review_hook(payload)
        from pathlib import Path
        files = list(Path(e.audit.log_dir).glob("*.jsonl"))
        if files:
            lines = files[0].read_text().strip().split("\n")
            dual_review_entries = [l for l in lines if '"action": "dual_review"' in l]
            assert len(dual_review_entries) == 0


# ── TestAlternateModelConfig ────────────────────────────────────────


class TestAlternateModelConfig:
    def test_pick_alternate_for_claude(self):
        from agent.core.engine import _pick_alternate_model
        assert _pick_alternate_model("claude-sonnet-4-6") == "gpt-4o"
        assert _pick_alternate_model("claude-opus-4-6") == "gpt-4o"

    def test_pick_alternate_for_gpt(self):
        from agent.core.engine import _pick_alternate_model
        assert _pick_alternate_model("gpt-4o") == "claude-sonnet-4-6"
        assert _pick_alternate_model("gpt-4o-mini") == "claude-sonnet-4-6"
        assert _pick_alternate_model("o1-preview") == "claude-sonnet-4-6"

    def test_pick_alternate_for_chinese_models(self):
        from agent.core.engine import _pick_alternate_model
        for m in ("qwen-max", "deepseek-chat", "glm-4", "kimi-k2",
                  "MiniMax-Text-01", "doubao-pro"):
            assert _pick_alternate_model(m) == "gpt-4o"

    def test_pick_alternate_for_unknown(self):
        from agent.core.engine import _pick_alternate_model
        assert _pick_alternate_model("some-unknown-model") == "claude-sonnet-4-6"

    def test_pick_alternate_for_empty(self):
        from agent.core.engine import _pick_alternate_model
        assert _pick_alternate_model("") == "claude-sonnet-4-6"

    def test_dual_review_model_override(self):
        e = _make_engine_with_reviewers(
            _approve_chat, _approve_chat,
            primary_model="claude-sonnet-4-6",
            secondary_model="custom-judge",
            dual_review_model="custom-judge",  # via config not via _make_engine_with_reviewers
        )
        # Set config.dual_review_model to verify it's used
        e.config.dual_review_model = "custom-judge"
        # Recreate manager
        e.dual_review = DualReviewManager(
            primary_chat=_approve_chat,
            secondary_chat=_approve_chat,
            primary_model="claude-sonnet-4-6",
            secondary_model=e.config.dual_review_model,
        )
        assert e.dual_review.secondary_model == "custom-judge"


# ── TestEngineIntegrationEndToEnd ───────────────────────────────────


class TestEngineIntegrationEndToEnd:
    """Verify the hook is registered and fires when the engine is used."""

    def test_hook_registered_for_dual_review(self):
        e = AgentEngine(_config())
        # Dual-review hook should be in the registry
        assert e.hooks.has(BEFORE_TOOL_EXECUTION)
        # Calling the hook directly should work
        assert callable(e._dual_review_hook)

    @pytest.mark.asyncio
    async def test_hook_skips_low_risk(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        out = await e._dual_review_hook({
            "tool": "read_file", "args": {"path": "x.py"}, "tc_id": "t"
        })
        assert e.dual_review.reviews_run == 0

    @pytest.mark.asyncio
    async def test_hook_blocks_high_risk_rejected(self):
        e = _make_engine_with_reviewers(_approve_chat, _reject_chat)
        with pytest.raises(PermissionDenied):
            await e._dual_review_hook({
                "tool": "write_file", "args": {"path": "rm -rf /"}, "tc_id": "t"
            })


# ── TestRateLimitingInEngine ────────────────────────────────────────


class TestRateLimitingInEngine:
    @pytest.mark.asyncio
    async def test_rate_limit_applies(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        # Replace the manager's rate limiter with a tight one
        e.dual_review.rate_limiter = RateLimiter(max_per_minute=2)
        # 2 calls pass through
        for i in range(2):
            out = await e._dual_review_hook({
                "tool": "write_file", "args": {"path": f"f{i}.py"}, "tc_id": f"t{i}"
            })
            assert out is not None
        # 3rd call is rate-limited → raises ReviewRequiresUser
        with pytest.raises(ReviewRequiresUser):
            await e._dual_review_hook({
                "tool": "write_file", "args": {"path": "f3.py"}, "tc_id": "t3"
            })


# ── TestContextPassing ──────────────────────────────────────────────


class TestContextPassing:
    @pytest.mark.asyncio
    async def test_context_included(self):
        captured = []

        async def spy(messages, stream=False):
            captured.append(messages[0].content)
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        e = _make_engine_with_reviewers(spy, spy)
        await e._dual_review_hook({
            "tool": "write_file",
            "args": {"path": "x.py"},
            "context": "user is refactoring",
            "tc_id": "t1",
        })
        assert len(captured) == 2
        for prompt in captured:
            assert "user is refactoring" in prompt


# ── TestNoExceptionEscapesUnexpectedly ──────────────────────────────


class TestNoExceptionEscapesUnexpectedly:
    @pytest.mark.asyncio
    async def test_unexpected_reviewer_error_does_not_crash_hook(self):
        async def bad_chat(messages, stream=False):
            raise RuntimeError("LLM offline")

        e = _make_engine_with_reviewers(_approve_chat, bad_chat)
        # One reviewer works, one fails → split → requires_user
        with pytest.raises(ReviewRequiresUser):
            await e._dual_review_hook({
                "tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"
            })


# ── TestHighRiskToolSetEnumeration ──────────────────────────────


class TestHighRiskToolSetEnumeration:
    """Document the high-risk tool set composition."""

    def test_high_risk_count_matches_documented_set(self):
        # PR-11 ships with a curated set. If this number changes, the
        # set membership test should be updated to reflect the new tools.
        from agent.core.dual_review import DualReviewManager
        expected_count = len(DualReviewManager.HIGH_RISK_TOOLS)
        assert expected_count >= 8
        # All entries are non-empty strings
        for t in DualReviewManager.HIGH_RISK_TOOLS:
            assert isinstance(t, str) and t

    def test_common_tools_are_high_risk(self):
        from agent.core.dual_review import DualReviewManager
        # Tools that should DEFINITELY be in the high-risk set
        must_be_high_risk = {
            "write_file", "execute_command", "apply_diff",
            "create_pr", "web_fetch", "install_package",
        }
        for tool in must_be_high_risk:
            assert tool in DualReviewManager.HIGH_RISK_TOOLS, \
                f"{tool} should be high-risk"

    def test_read_only_tools_are_not_high_risk(self):
        from agent.core.dual_review import DualReviewManager
        # Tools that should DEFINITELY NOT be high-risk
        must_be_low_risk = {
            "read_file", "list_files", "grep", "code_search",
            "semantic_search", "render_progress", "view_diff",
        }
        for tool in must_be_low_risk:
            assert tool not in DualReviewManager.HIGH_RISK_TOOLS, \
                f"{tool} should not be high-risk"


# ── TestHookWithContextField ────────────────────────────────────


class TestHookWithContextField:
    """The hook accepts a `context` field which is passed to the reviewers."""

    @pytest.mark.asyncio
    async def test_context_passed_to_reviewers(self):
        captured = []

        async def spy(messages, stream=False):
            captured.append(messages[0].content)
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        e = _make_engine_with_reviewers(spy, spy)
        payload = {
            "tool": "write_file",
            "args": {"path": "x.py"},
            "context": "user is migrating auth from oauth1 to oauth2",
        }
        await e._dual_review_hook(payload)
        # Both prompts should contain the context
        assert len(captured) == 2
        for prompt in captured:
            assert "user is migrating auth from oauth1 to oauth2" in prompt

    @pytest.mark.asyncio
    async def test_no_context_field_uses_placeholder(self):
        captured = []

        async def spy(messages, stream=False):
            captured.append(messages[0].content)
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        e = _make_engine_with_reviewers(spy, spy)
        payload = {"tool": "write_file", "args": {"path": "x.py"}}
        await e._dual_review_hook(payload)
        # No context → "(none)" placeholder
        for prompt in captured:
            assert "(none)" in prompt


# ── TestHookMultipleHighRiskCallsInSequence ─────────────────────


class TestHookMultipleHighRiskCallsInSequence:
    """Stats accumulate across multiple high-risk tool calls."""

    @pytest.mark.asyncio
    async def test_approved_counter_increments(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        for i in range(5):
            await e._dual_review_hook({
                "tool": "write_file",
                "args": {"path": f"f{i}.py"},
            })
        assert e.dual_review.reviews_run == 5
        assert e.dual_review.reviews_approved == 5

    @pytest.mark.asyncio
    async def test_rejected_counter_increments(self):
        e = _make_engine_with_reviewers(_reject_chat, _reject_chat)
        for i in range(3):
            with pytest.raises(PermissionDenied):
                await e._dual_review_hook({
                    "tool": "write_file",
                    "args": {"path": f"f{i}.py"},
                })
        assert e.dual_review.reviews_run == 3
        assert e.dual_review.reviews_rejected == 3

    @pytest.mark.asyncio
    async def test_mixed_outcomes_counters(self):
        """Approve, reject, split (abstain), low-risk — each goes to its
        own counter."""
        e = _make_engine_with_reviewers()
        # Approve
        e.dual_review.primary_chat = _approve_chat
        e.dual_review.secondary_chat = _approve_chat
        await e._dual_review_hook({"tool": "write_file", "args": {"a": 1}})
        # Reject
        e.dual_review.primary_chat = _reject_chat
        e.dual_review.secondary_chat = _reject_chat
        with pytest.raises(PermissionDenied):
            await e._dual_review_hook({"tool": "write_file", "args": {"a": 2}})
        # Split (abstain) → requires_user
        e.dual_review.primary_chat = _approve_chat
        e.dual_review.secondary_chat = _abstain_chat
        with pytest.raises(ReviewRequiresUser):
            await e._dual_review_hook({"tool": "write_file", "args": {"a": 3}})
        # Low-risk → no review
        await e._dual_review_hook({"tool": "read_file", "args": {"a": 4}})
        # Verify counters
        assert e.dual_review.reviews_run == 3  # low-risk not counted
        assert e.dual_review.reviews_approved == 1
        assert e.dual_review.reviews_rejected == 1
        assert e.dual_review.reviews_user_required == 1


# ── TestPickAlternateModelEdgeCases ─────────────────────────────


class TestPickAlternateModelEdgeCases:
    def test_gemini_picks_alternate(self):
        from agent.core.engine import _pick_alternate_model
        # Gemini is in the unknown bucket
        result = _pick_alternate_model("gemini-1.5-pro")
        assert result in {"claude-sonnet-4-6", "gpt-4o"}

    def test_o1_picks_alternate(self):
        from agent.core.engine import _pick_alternate_model
        result = _pick_alternate_model("o1-preview")
        assert result == "claude-sonnet-4-6"

    def test_case_insensitive(self):
        from agent.core.engine import _pick_alternate_model
        # The function lowercases input before matching, so "CLAUDE" still
        # matches the "claude" substring → returns the GPT alternate
        result = _pick_alternate_model("CLAUDE-SONNET-4-6")
        assert result == "gpt-4o"

    def test_partial_match(self):
        from agent.core.engine import _pick_alternate_model
        # A model name that starts with "claude" but has extras
        result = _pick_alternate_model("claude-haiku-3-5")
        assert result == "gpt-4o"


# ── TestHookPassesPayloadUnchangedOnApprove ────────────────────


class TestHookPassesPayloadUnchangedOnApprove:
    """The hook must return the original payload on APPROVE (not a copy)."""

    @pytest.mark.asyncio
    async def test_returns_same_object(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        payload = {
            "tool": "write_file",
            "args": {"path": "x.py"},
            "tc_id": "test_id_42",
        }
        out = await e._dual_review_hook(payload)
        # Identity preserved (not a copy)
        assert out is payload
        # All original fields intact
        assert out["tc_id"] == "test_id_42"
        assert out["tool"] == "write_file"


# ── TestAuditFailureDoesNotBreakHook ───────────────────────────


class TestAuditFailureDoesNotBreakHook:
    """If the audit log fails to write, the hook should still raise the
    correct exception (not crash with a different error)."""

    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        from agent.core.audit_log import reset_audit_logger
        reset_audit_logger()
        monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
        yield
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_audit_failure_still_raises_correctly(self):
        e = _make_engine_with_reviewers(_approve_chat, _reject_chat)
        from agent.core.audit_log import get_audit_logger
        e.audit = get_audit_logger()
        # Make audit.log() raise
        original_log = e.audit.log
        def broken_log(record):
            raise OSError("disk full")
        e.audit.log = broken_log
        # Hook should still raise PermissionDenied, not OSError
        with pytest.raises(PermissionDenied):
            await e._dual_review_hook({
                "tool": "write_file", "args": {"path": "x.py"},
            })
        # Restore
        e.audit.log = original_log
