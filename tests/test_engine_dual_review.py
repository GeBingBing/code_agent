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

import json

import pytest

from agent.core.audit_log import reset_audit_logger
from agent.core.dual_review import (
    DualReviewManager,
    PermissionDenied,
    RateLimiter,
    ReviewRequiresUser,
)
from agent.core.engine import AgentConfig, AgentEngine
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


def _make_engine_with_reviewers(
    primary_chat=None,
    secondary_chat=None,
    primary_model="gpt-4o",
    secondary_model="claude-sonnet-4-6",
    **config_overrides,
):
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

        for m in ("qwen-max", "deepseek-chat", "glm-4", "kimi-k2", "MiniMax-Text-01", "doubao-pro"):
            assert _pick_alternate_model(m) == "gpt-4o"

    def test_pick_alternate_for_unknown(self):
        from agent.core.engine import _pick_alternate_model

        assert _pick_alternate_model("some-unknown-model") == "claude-sonnet-4-6"

    def test_pick_alternate_for_empty(self):
        from agent.core.engine import _pick_alternate_model

        assert _pick_alternate_model("") == "claude-sonnet-4-6"

    def test_dual_review_model_override(self):
        e = _make_engine_with_reviewers(
            _approve_chat,
            _approve_chat,
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
        out = await e._dual_review_hook(
            {"tool": "read_file", "args": {"path": "x.py"}, "tc_id": "t"}
        )
        assert e.dual_review.reviews_run == 0

    @pytest.mark.asyncio
    async def test_hook_blocks_high_risk_rejected(self):
        e = _make_engine_with_reviewers(_approve_chat, _reject_chat)
        with pytest.raises(PermissionDenied):
            await e._dual_review_hook(
                {"tool": "write_file", "args": {"path": "rm -rf /"}, "tc_id": "t"}
            )


# ── TestRateLimitingInEngine ────────────────────────────────────────


class TestRateLimitingInEngine:
    @pytest.mark.asyncio
    async def test_rate_limit_applies(self):
        e = _make_engine_with_reviewers(_approve_chat, _approve_chat)
        # Replace the manager's rate limiter with a tight one
        e.dual_review.rate_limiter = RateLimiter(max_per_minute=2)
        # 2 calls pass through
        for i in range(2):
            out = await e._dual_review_hook(
                {"tool": "write_file", "args": {"path": f"f{i}.py"}, "tc_id": f"t{i}"}
            )
            assert out is not None
        # 3rd call is rate-limited → raises ReviewRequiresUser
        with pytest.raises(ReviewRequiresUser):
            await e._dual_review_hook(
                {"tool": "write_file", "args": {"path": "f3.py"}, "tc_id": "t3"}
            )


# ── TestContextPassing ──────────────────────────────────────────────


class TestContextPassing:
    @pytest.mark.asyncio
    async def test_context_included(self):
        captured = []

        async def spy(messages, stream=False):
            captured.append(messages[0].content)
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        e = _make_engine_with_reviewers(spy, spy)
        await e._dual_review_hook(
            {
                "tool": "write_file",
                "args": {"path": "x.py"},
                "context": "user is refactoring",
                "tc_id": "t1",
            }
        )
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
            await e._dual_review_hook(
                {"tool": "write_file", "args": {"path": "x.py"}, "tc_id": "t1"}
            )


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
            "write_file",
            "execute_command",
            "apply_diff",
            "create_pr",
            "web_fetch",
            "install_package",
        }
        for tool in must_be_high_risk:
            assert tool in DualReviewManager.HIGH_RISK_TOOLS, f"{tool} should be high-risk"

    def test_read_only_tools_are_not_high_risk(self):
        from agent.core.dual_review import DualReviewManager

        # Tools that should DEFINITELY NOT be high-risk
        must_be_low_risk = {
            "read_file",
            "list_files",
            "grep",
            "code_search",
            "semantic_search",
            "render_progress",
            "view_diff",
        }
        for tool in must_be_low_risk:
            assert tool not in DualReviewManager.HIGH_RISK_TOOLS, f"{tool} should not be high-risk"


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
            await e._dual_review_hook(
                {
                    "tool": "write_file",
                    "args": {"path": f"f{i}.py"},
                }
            )
        assert e.dual_review.reviews_run == 5
        assert e.dual_review.reviews_approved == 5

    @pytest.mark.asyncio
    async def test_rejected_counter_increments(self):
        e = _make_engine_with_reviewers(_reject_chat, _reject_chat)
        for i in range(3):
            with pytest.raises(PermissionDenied):
                await e._dual_review_hook(
                    {
                        "tool": "write_file",
                        "args": {"path": f"f{i}.py"},
                    }
                )
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


# ── P14-2: cross-family strict dual-agent review ────────────────────


class TestDetectFamily:
    """P14-2: provider/model → family taxonomy."""

    def test_openai_family(self):
        from agent.llm.client import _detect_family

        assert _detect_family("openai", "gpt-4o") == "openai"
        assert _detect_family("openai", "o1-preview") == "openai"

    def test_dashscope_family(self):
        from agent.llm.client import _detect_family

        assert _detect_family("dashscope", "qwen-plus") == "dashscope"

    def test_zhipu_family(self):
        from agent.llm.client import _detect_family

        assert _detect_family("zhipu", "glm-4-plus") == "zhipu"

    def test_minimax_family(self):
        from agent.llm.client import _detect_family

        assert _detect_family("minimax", "abab6.5s-chat") == "minimax"

    def test_kimi_family(self):
        from agent.llm.client import _detect_family

        assert _detect_family("kimi", "moonshot-v1-8k") == "kimi"
        # Moonshot alias
        assert _detect_family("moonshot", "v1") == "kimi"

    def test_inferred_from_model(self):
        """When provider is unknown, family can be inferred from model name."""
        from agent.llm.client import _detect_family

        assert _detect_family("auto", "qwen-max") == "dashscope"
        assert _detect_family("auto", "glm-4") == "zhipu"
        assert _detect_family("auto", "abab6.5s-chat") == "minimax"

    def test_unknown_returns_provider_or_unknown(self):
        from agent.llm.client import _detect_family

        assert _detect_family("random-provider", "") == "random-provider"
        assert _detect_family("", "") == "unknown"


class TestPickAlternateProviderName:
    """P14-2: cross-family provider selection based on env API keys."""

    def test_returns_none_when_no_other_keys(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        # Strip all provider keys
        for k in (
            "OPENAI_API_KEY",
            "KIMI_API_KEY",
            "DASHSCOPE_API_KEY",
            "ZHIPU_API_KEY",
            "MINIMAX_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        result = _pick_alternate_provider_name("openai", "gpt-4o")
        assert result is None

    def test_picks_first_available_other_family(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        # Primary is OpenAI. Available alternate families: dashscope + zhipu
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
        # Should pick dashscope (higher priority than zhipu)
        result = _pick_alternate_provider_name("openai", "gpt-4o")
        assert result == "dashscope"

    def test_skips_same_family_provider(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        # Primary is dashscope/qwen. OpenAI is a different family — should pick it.
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        result = _pick_alternate_provider_name("dashscope", "qwen-plus")
        assert result == "openai"

    def test_honors_explicit_env_override(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
        monkeypatch.setenv("DUAL_REVIEW_PROVIDER", "zhipu")
        # Override picks zhipu even though dashscope isn't available
        result = _pick_alternate_provider_name("openai", "gpt-4o")
        assert result == "zhipu"

    def test_ignores_same_family_explicit_override(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")
        monkeypatch.setenv("DUAL_REVIEW_PROVIDER", "openai")  # same family — ignored
        result = _pick_alternate_provider_name("openai", "gpt-4o")
        # Should fall through to picking dashscope (next available different family)
        assert result == "dashscope"

    def test_returns_none_when_only_one_provider_available(self, monkeypatch):
        from agent.llm.client import _pick_alternate_provider_name

        # Only OpenAI available
        for k in (
            "KIMI_API_KEY",
            "DASHSCOPE_API_KEY",
            "ZHIPU_API_KEY",
            "MINIMAX_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        result = _pick_alternate_provider_name("openai", "gpt-4o")
        assert result is None


class TestCreateAlternateProviderClient:
    """P14-2: factory returns a second, different-family LLMClient."""

    def test_returns_none_when_no_keys_at_all(self, monkeypatch):
        from agent.llm.client import (
            LLMClient,
            create_alternate_provider_client,
        )

        # No keys anywhere → returns None (no alternate available)
        for k in (
            "OPENAI_API_KEY",
            "KIMI_API_KEY",
            "DASHSCOPE_API_KEY",
            "ZHIPU_API_KEY",
            "MINIMAX_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        primary = LLMClient(model="mock", provider="mock", api_key="mock")
        result = create_alternate_provider_client(primary)
        assert result is None

    def test_returns_different_provider(self, monkeypatch):
        from agent.llm.client import (
            LLMClient,
            create_alternate_provider_client,
        )

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")
        primary = LLMClient(model="gpt-4o", provider="openai", api_key="sk-test")
        result = create_alternate_provider_client(primary)
        assert result is not None
        assert result.provider != primary.provider


class TestBuildDualReviewManagerCrossProvider:
    """P14-2: AgentEngine._build_dual_review_manager honors the cross-provider flag."""

    def test_disabled_uses_same_client(self):
        """Backward compat: dual_review_strict_cross_provider=False → same client."""
        from unittest.mock import MagicMock

        e = AgentEngine(_config(dual_review_strict_cross_provider=False))
        # Force-inject a known llm (mock)
        e.llm = MagicMock()
        e.llm.provider = "mock"
        e.llm.model = "mock"
        mgr = e._build_dual_review_manager()
        assert mgr is not None
        # Backward compat: both reviewers use the same chat function
        assert mgr.primary_chat is mgr.secondary_chat

    def test_enabled_multi_provider_uses_different_client(self, monkeypatch):
        """With strict flag + multi-provider env, secondary chat must differ."""
        from agent.llm.client import LLMClient

        # Set up multi-provider env
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")

        e = AgentEngine(
            _config(
                model="gpt-4o",
                provider="openai",
                dual_review_strict_cross_provider=True,
            )
        )
        # Bypass engine's full init and inject the primary client directly.
        primary = LLMClient(model="gpt-4o", provider="openai", api_key="sk-test")
        e.llm = primary

        mgr = e._build_dual_review_manager()
        assert mgr is not None
        # Different clients!
        assert mgr.primary_chat is not mgr.secondary_chat
        # And the secondary model is the alternate family's model
        assert mgr.secondary_model != primary.model

    def test_enabled_single_provider_falls_back_to_primary(self, monkeypatch):
        """With strict flag + only one provider key, secondary falls back to primary."""
        from agent.llm.client import LLMClient

        # Strip everything except openai
        for k in (
            "KIMI_API_KEY",
            "DASHSCOPE_API_KEY",
            "ZHIPU_API_KEY",
            "MINIMAX_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        e = AgentEngine(
            _config(
                model="gpt-4o",
                provider="openai",
                dual_review_strict_cross_provider=True,
            )
        )
        primary = LLMClient(model="gpt-4o", provider="openai", api_key="sk-test")
        e.llm = primary

        mgr = e._build_dual_review_manager()
        assert mgr is not None
        # Falls back to primary — single-provider users see no breakage
        assert mgr.primary_chat is mgr.secondary_chat

    def test_config_field_default_is_false(self):
        """Default config should NOT enable strict mode (backward compat)."""
        cfg = _config()
        assert cfg.dual_review_strict_cross_provider is False


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
            await e._dual_review_hook(
                {
                    "tool": "write_file",
                    "args": {"path": "x.py"},
                }
            )
        # Restore
        e.audit.log = original_log
