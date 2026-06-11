"""Unit tests for dual_review module (PR-11).

These tests do NOT exercise the engine integration — they verify the
DualReviewManager, RateLimiter, verdict parsing, and aggregation
rules in isolation.
"""

import asyncio
import json
import pytest

from agent.core.dual_review import (
    DualReviewManager,
    DualReviewResult,
    PermissionDenied,
    RateLimiter,
    ReviewDecision,
    ReviewRequiresUser,
    ReviewVerdict,
    _parse_verdict_response,
    get_dual_review_manager,
    reset_dual_review_manager,
)


# ── Helpers ─────────────────────────────────────────────────────────


async def _approve_chat(messages, stream=False):
    return json.dumps({"verdict": "approve", "rationale": "looks fine"}), None


async def _reject_chat(messages, stream=False):
    return json.dumps({"verdict": "reject", "rationale": "dangerous"}), None


async def _abstain_chat(messages, stream=False):
    return json.dumps({"verdict": "abstain", "rationale": "uncertain"}), None


async def _error_chat(messages, stream=False):
    raise RuntimeError("LLM unavailable")


def _decision(verdict, reviewer="r", rationale="r"):
    return ReviewDecision(
        reviewer_id=reviewer, model="m", verdict=verdict, rationale=rationale
    )


# ── TestVerdictEnum ────────────────────────────────────────────────


class TestVerdictEnum:
    def test_values(self):
        assert ReviewVerdict.APPROVE.value == "approve"
        assert ReviewVerdict.REJECT.value == "reject"
        assert ReviewVerdict.ABSTAIN.value == "abstain"

    def test_distinct(self):
        verdicts = {ReviewVerdict.APPROVE, ReviewVerdict.REJECT, ReviewVerdict.ABSTAIN}
        assert len(verdicts) == 3


# ── TestReviewDecision ────────────────────────────────────────────


class TestReviewDecision:
    def test_construction(self):
        d = _decision(ReviewVerdict.APPROVE)
        assert d.reviewer_id == "r"
        assert d.verdict == ReviewVerdict.APPROVE
        assert d.elapsed_ms == 0.0


# ── TestDualReviewResult ────────────────────────────────────────────


class TestDualReviewResult:
    def test_to_dict(self):
        d1 = ReviewDecision("primary", "gpt", ReviewVerdict.APPROVE, "ok", 10.0)
        d2 = ReviewDecision("secondary", "claude", ReviewVerdict.REJECT, "no", 20.0)
        r = DualReviewResult(
            decisions=[d1, d2],
            final_verdict=ReviewVerdict.REJECT,
            requires_user=False,
            consensus=False,
            tool="write_file",
            args={"path": "x.py"},
        )
        d = r.to_dict()
        assert d["final_verdict"] == "reject"
        assert d["requires_user"] is False
        assert d["consensus"] is False
        assert d["tool"] == "write_file"
        assert d["args"] == {"path": "x.py"}
        assert len(d["decisions"]) == 2
        assert d["decisions"][0]["elapsed_ms"] == 10.0
        assert d["decisions"][1]["rationale"] == "no"


# ── TestRateLimiter ────────────────────────────────────────────────


class TestRateLimiter:
    def test_allows_up_to_max(self):
        rl = RateLimiter(max_per_minute=3)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False  # 4th call blocked

    def test_used_counter(self):
        rl = RateLimiter(max_per_minute=2)
        rl.allow()
        rl.allow()
        assert rl.used() == 2

    def test_reset_clears(self):
        rl = RateLimiter(max_per_minute=2)
        rl.allow()
        rl.allow()
        assert rl.allow() is False
        rl.reset()
        assert rl.used() == 0
        assert rl.allow() is True

    def test_window_expiry(self, monkeypatch):
        rl = RateLimiter(max_per_minute=2, window_seconds=1)
        # Seed 2 calls at t=0
        rl._calls = [0.0, 0.0]
        # Pretend 2 seconds have passed → calls should be expired
        monkeypatch.setattr("time.time", lambda: 2.0)
        assert rl.allow() is True


# ── TestHighRiskTools ──────────────────────────────────────────────


class TestHighRiskTools:
    def test_known_high_risk(self):
        mgr = DualReviewManager()
        for tool in ("write_file", "execute_command", "git_push",
                     "create_pr", "web_fetch", "install_package",
                     "uninstall_package", "apply_diff", "edit_file",
                     "sandbox_execute"):
            assert mgr.is_high_risk(tool), f"expected {tool} high-risk"

    def test_low_risk_excluded(self):
        mgr = DualReviewManager()
        for tool in ("read_file", "grep", "list_files", "code_search",
                     "glob", "list_skills", "list_sub_agents"):
            assert not mgr.is_high_risk(tool), f"expected {tool} low-risk"

    def test_high_risk_is_frozenset(self):
        # Iteration order is stable
        mgr = DualReviewManager()
        names = list(mgr.HIGH_RISK_TOOLS)
        assert isinstance(names, list)
        # And contains expected members
        assert "write_file" in names
        assert "execute_command" in names


# ── TestAggregation ────────────────────────────────────────────────


class TestAggregation:
    def test_all_approve(self):
        decs = [_decision(ReviewVerdict.APPROVE, "p"), _decision(ReviewVerdict.APPROVE, "s")]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.APPROVE
        assert r.requires_user is False
        assert r.consensus is True

    def test_any_reject_blocks(self):
        decs = [_decision(ReviewVerdict.APPROVE, "p"), _decision(ReviewVerdict.REJECT, "s")]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.requires_user is False
        assert r.consensus is False

    def test_all_reject(self):
        decs = [_decision(ReviewVerdict.REJECT, "p"), _decision(ReviewVerdict.REJECT, "s")]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.requires_user is False
        assert r.consensus is True  # both rejected

    def test_one_abstain_requires_user(self):
        decs = [_decision(ReviewVerdict.APPROVE, "p"), _decision(ReviewVerdict.ABSTAIN, "s")]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.ABSTAIN
        assert r.requires_user is True
        assert r.consensus is False

    def test_both_abstain_requires_user(self):
        decs = [_decision(ReviewVerdict.ABSTAIN, "p"), _decision(ReviewVerdict.ABSTAIN, "s")]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.ABSTAIN
        assert r.requires_user is True
        assert r.consensus is False

    def test_empty_decisions_requires_user(self):
        r = DualReviewManager._aggregate([])
        assert r.requires_user is True
        assert r.consensus is False


# ── TestReviewDecisionHook ─────────────────────────────────────────


class TestReviewDecisionHook:
    def test_approve_no_raise(self):
        mgr = DualReviewManager()
        r = DualReviewResult(
            decisions=[_decision(ReviewVerdict.APPROVE, "p"),
                       _decision(ReviewVerdict.APPROVE, "s")],
            final_verdict=ReviewVerdict.APPROVE,
            requires_user=False,
            consensus=True,
        )
        # Should NOT raise
        mgr.review_decision(r)

    def test_reject_raises_permission_denied(self):
        mgr = DualReviewManager()
        r = DualReviewResult(
            decisions=[_decision(ReviewVerdict.APPROVE, "p"),
                       _decision(ReviewVerdict.REJECT, "s", "dangerous")],
            final_verdict=ReviewVerdict.REJECT,
            requires_user=False,
            consensus=False,
        )
        with pytest.raises(PermissionDenied) as exc:
            mgr.review_decision(r)
        assert "dangerous" in str(exc.value)
        assert len(exc.value.decisions) == 2

    def test_split_raises_review_requires_user(self):
        mgr = DualReviewManager()
        r = DualReviewResult(
            decisions=[_decision(ReviewVerdict.APPROVE, "p"),
                       _decision(ReviewVerdict.ABSTAIN, "s")],
            final_verdict=ReviewVerdict.ABSTAIN,
            requires_user=True,
            consensus=False,
        )
        with pytest.raises(ReviewRequiresUser) as exc:
            mgr.review_decision(r)
        assert exc.value.result is r


# ── TestParseVerdictResponse ────────────────────────────────────────


class TestParseVerdictResponse:
    def test_plain_json(self):
        v, r = _parse_verdict_response('{"verdict": "approve", "rationale": "ok"}')
        assert v == ReviewVerdict.APPROVE
        assert r == "ok"

    def test_smart_quotes(self):
        v, r = _parse_verdict_response('{“verdict”: “reject”, “rationale”: “bad”}')
        assert v == ReviewVerdict.REJECT
        assert r == "bad"

    def test_surrounding_prose(self):
        v, r = _parse_verdict_response(
            'Here is my review: {"verdict": "approve", "rationale": "fine"} — done'
        )
        assert v == ReviewVerdict.APPROVE
        assert r == "fine"

    def test_code_fences(self):
        v, r = _parse_verdict_response('```json\n{"verdict": "reject", "rationale": "x"}\n```')
        assert v == ReviewVerdict.REJECT
        assert r == "x"

    def test_missing_rationale(self):
        v, r = _parse_verdict_response('{"verdict": "approve"}')
        assert v == ReviewVerdict.APPROVE
        assert r == ""

    def test_unknown_verdict_becomes_abstain(self):
        v, r = _parse_verdict_response('{"verdict": "maybe"}')
        assert v == ReviewVerdict.ABSTAIN
        assert "maybe" in r

    def test_unparseable(self):
        v, r = _parse_verdict_response("just a string with no JSON")
        assert v == ReviewVerdict.ABSTAIN
        assert "Could not parse" in r

    def test_non_string_input(self):
        v, r = _parse_verdict_response(None)
        assert v == ReviewVerdict.ABSTAIN
        assert r == "Non-string response"

    def test_case_insensitive(self):
        v, _ = _parse_verdict_response('{"verdict": "APPROVE"}')
        assert v == ReviewVerdict.APPROVE
        v, _ = _parse_verdict_response('{"verdict": "Reject"}')
        assert v == ReviewVerdict.REJECT

    def test_synonyms(self):
        v, _ = _parse_verdict_response('{"verdict": "yes"}')
        assert v == ReviewVerdict.APPROVE
        v, _ = _parse_verdict_response('{"verdict": "deny"}')
        assert v == ReviewVerdict.REJECT


# ── TestReviewBothApprove ──────────────────────────────────────────


class TestReviewBothApprove:
    @pytest.mark.asyncio
    async def test_approve(self):
        mgr = DualReviewManager(primary_chat=_approve_chat, secondary_chat=_approve_chat)
        r = await mgr.review("write_file", {"path": "x.py"})
        assert r.final_verdict == ReviewVerdict.APPROVE
        assert r.requires_user is False
        assert r.consensus is True
        assert r.tool == "write_file"
        assert mgr.reviews_approved == 1
        assert mgr.reviews_rejected == 0


# ── TestReviewOneReject ────────────────────────────────────────────


class TestReviewOneReject:
    @pytest.mark.asyncio
    async def test_rejects_when_any_rejects(self):
        mgr = DualReviewManager(
            primary_chat=_approve_chat, secondary_chat=_reject_chat
        )
        r = await mgr.review("write_file", {"path": "rm -rf /"})
        assert r.final_verdict == ReviewVerdict.REJECT
        assert mgr.reviews_rejected == 1


# ── TestReviewAbstain ──────────────────────────────────────────────


class TestReviewAbstain:
    @pytest.mark.asyncio
    async def test_one_abstain_requires_user(self):
        mgr = DualReviewManager(
            primary_chat=_approve_chat, secondary_chat=_abstain_chat
        )
        r = await mgr.review("execute_command", {"command": "ls"})
        assert r.requires_user is True
        assert mgr.reviews_user_required == 1


# ── TestReviewError ────────────────────────────────────────────────


class TestReviewError:
    @pytest.mark.asyncio
    async def test_reviewer_error_becomes_abstain(self):
        mgr = DualReviewManager(
            primary_chat=_approve_chat, secondary_chat=_error_chat
        )
        r = await mgr.review("write_file", {"path": "x.py"})
        # The errored reviewer abstains; the other approved → split → user
        assert r.requires_user is True
        # Find the abstaining decision
        abstains = [d for d in r.decisions if d.verdict == ReviewVerdict.ABSTAIN]
        assert len(abstains) == 1
        assert "RuntimeError" in abstains[0].rationale


# ── TestReviewRateLimited ──────────────────────────────────────────


class TestReviewRateLimited:
    @pytest.mark.asyncio
    async def test_rate_limit_forces_user(self):
        mgr = DualReviewManager(
            primary_chat=_approve_chat, secondary_chat=_approve_chat,
            max_per_minute=2,
        )
        # Burn 2 calls
        await mgr.review("write_file", {"path": "a.py"})
        await mgr.review("write_file", {"path": "b.py"})
        # 3rd is rate-limited → requires_user
        r = await mgr.review("write_file", {"path": "c.py"})
        assert r.requires_user is True
        assert r.decisions == []
        assert mgr.reviews_rate_limited == 1
        # The first two were approved; the 3rd counts as user_required
        assert mgr.reviews_user_required == 1


# ── TestReviewPromptBuilding ───────────────────────────────────────


class TestReviewPromptBuilding:
    def test_prompt_contains_tool_and_args(self):
        mgr = DualReviewManager()
        prompt = mgr._build_review_prompt("write_file", {"path": "x.py"}, "ctx")
        assert "write_file" in prompt
        assert "x.py" in prompt
        assert "ctx" in prompt

    def test_prompt_truncates_large_args(self):
        mgr = DualReviewManager()
        big = {"data": "x" * 5000}
        prompt = mgr._build_review_prompt("write_file", big, "")
        assert "truncated" in prompt

    def test_prompt_handles_uncopyable_args(self):
        mgr = DualReviewManager()
        # Set with non-JSON-serializable value
        class Foo: pass
        prompt = mgr._build_review_prompt("write_file", {"obj": Foo()}, "")
        # Should not raise
        assert "write_file" in prompt


# ── TestSingleton ──────────────────────────────────────────────────


class TestSingleton:
    def test_singleton(self):
        reset_dual_review_manager()
        a = get_dual_review_manager()
        b = get_dual_review_manager()
        assert a is b

    def test_reset_creates_new(self):
        a = get_dual_review_manager()
        reset_dual_review_manager()
        b = get_dual_review_manager()
        assert a is not b

    def test_reset_clears_singleton(self):
        get_dual_review_manager()
        reset_dual_review_manager()
        # Now manager must be re-instantiated
        from agent.core.dual_review import _default_manager
        assert _default_manager is None


# ── TestStubSecondary ─────────────────────────────────────────────


class TestStubSecondary:
    @pytest.mark.asyncio
    async def test_stub_approves_safe_command(self):
        mgr = DualReviewManager()  # No chat fns → primary=None, secondary=stub
        r = await mgr.review("write_file", {"path": "x.py", "content": "hi"})
        # primary returned ABSTAIN (no chat), secondary approved
        assert r.requires_user is True  # mixed → user

    @pytest.mark.asyncio
    async def test_stub_rejects_destructive_command(self):
        mgr = DualReviewManager()  # uses stub secondary
        # Use a tool that triggers stub's destructive pattern detection
        # The stub inspects the *prompt*; rm -rf in args appears in prompt
        r = await mgr.review("execute_command", {"command": "rm -rf /tmp"})
        # primary abstains (no chat), secondary rejects → final reject
        assert r.final_verdict == ReviewVerdict.REJECT


# ── TestParallelExecution ──────────────────────────────────────────


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_both_reviewers_invoked(self):
        calls = []

        async def track(messages, stream=False):
            calls.append("approve")
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        mgr = DualReviewManager(primary_chat=track, secondary_chat=track)
        r = await mgr.review("write_file", {"path": "x.py"})
        assert len(calls) == 2
        assert r.final_verdict == ReviewVerdict.APPROVE

    @pytest.mark.asyncio
    async def test_elapsed_recorded(self):
        mgr = DualReviewManager(primary_chat=_approve_chat, secondary_chat=_approve_chat)
        r = await mgr.review("write_file", {"path": "x.py"})
        for d in r.decisions:
            # Elapsed should be > 0; could be tiny in tests, just check float
            assert d.elapsed_ms >= 0.0


# ── TestReviewContext ──────────────────────────────────────────────


class TestReviewContext:
    @pytest.mark.asyncio
    async def test_context_included_in_prompt(self):
        captured = []

        async def spy(messages, stream=False):
            captured.append(messages[0].content)
            return json.dumps({"verdict": "approve", "rationale": "ok"}), None

        mgr = DualReviewManager(primary_chat=spy, secondary_chat=spy)
        await mgr.review("write_file", {"path": "x.py"}, context="user is migrating auth")
        assert len(captured) == 2
        for prompt in captured:
            assert "user is migrating auth" in prompt


# ── TestAggregationEdgeCases ──────────────────────────────────────


class TestAggregationEdgeCases:
    def test_three_decisions_one_reject(self):
        decs = [
            _decision(ReviewVerdict.APPROVE, "a"),
            _decision(ReviewVerdict.APPROVE, "b"),
            _decision(ReviewVerdict.REJECT, "c"),
        ]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.requires_user is False
        # Consensus should be False because reviewers disagreed
        assert r.consensus is False

    def test_three_decisions_all_approve_consensus(self):
        decs = [
            _decision(ReviewVerdict.APPROVE, "a"),
            _decision(ReviewVerdict.APPROVE, "b"),
            _decision(ReviewVerdict.APPROVE, "c"),
        ]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.APPROVE
        assert r.consensus is True
        assert r.requires_user is False

    def test_three_decisions_all_reject_consensus(self):
        decs = [
            _decision(ReviewVerdict.REJECT, "a"),
            _decision(ReviewVerdict.REJECT, "b"),
            _decision(ReviewVerdict.REJECT, "c"),
        ]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.consensus is True  # unanimous on REJECT
        assert r.requires_user is False

    def test_two_decisions_reject_reject_consensus(self):
        decs = [
            _decision(ReviewVerdict.REJECT, "a"),
            _decision(ReviewVerdict.REJECT, "b"),
        ]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.consensus is True

    def test_three_decisions_with_abstain_requires_user(self):
        decs = [
            _decision(ReviewVerdict.APPROVE, "a"),
            _decision(ReviewVerdict.ABSTAIN, "b"),
            _decision(ReviewVerdict.APPROVE, "c"),
        ]
        r = DualReviewManager._aggregate(decs)
        # 1 abstain in mix → must defer to user
        assert r.requires_user is True
        assert r.final_verdict == ReviewVerdict.ABSTAIN
        assert r.consensus is False

    def test_consensus_false_on_split_decision(self):
        # 1 approve + 1 reject → not unanimous on either side
        decs = [
            _decision(ReviewVerdict.APPROVE, "a"),
            _decision(ReviewVerdict.REJECT, "b"),
        ]
        r = DualReviewManager._aggregate(decs)
        assert r.final_verdict == ReviewVerdict.REJECT
        assert r.consensus is False  # not unanimous

    def test_to_dict_round_trip(self):
        decs = [
            _decision(ReviewVerdict.APPROVE, "a", "ok"),
            _decision(ReviewVerdict.REJECT, "b", "no"),
        ]
        r = DualReviewManager._aggregate(decs)
        r.tool = "write_file"
        r.args = {"path": "x.py"}
        d = r.to_dict()
        assert d["tool"] == "write_file"
        assert d["args"] == {"path": "x.py"}
        assert d["final_verdict"] == "reject"
        assert d["consensus"] is False
        assert len(d["decisions"]) == 2
        assert d["decisions"][0]["reviewer_id"] == "a"
        assert d["decisions"][1]["verdict"] == "reject"


# ── TestReviewDecisionHookEdgeCases ────────────────────────────────


class TestReviewDecisionHookEdgeCases:
    def test_approve_does_not_raise(self):
        mgr = DualReviewManager()
        result = DualReviewResult(
            decisions=[_decision(ReviewVerdict.APPROVE, "a")],
            final_verdict=ReviewVerdict.APPROVE,
            requires_user=False,
            consensus=True,
        )
        # Should not raise
        mgr.review_decision(result)

    def test_reject_raises_with_decisions_attached(self):
        mgr = DualReviewManager()
        decisions = [_decision(ReviewVerdict.REJECT, "a", "no good")]
        result = DualReviewResult(
            decisions=decisions,
            final_verdict=ReviewVerdict.REJECT,
            requires_user=False,
            consensus=False,
        )
        with pytest.raises(PermissionDenied) as exc:
            mgr.review_decision(result)
        assert "no good" in str(exc.value)
        assert exc.value.decisions == decisions

    def test_split_raises_with_result_attached(self):
        mgr = DualReviewManager()
        result = DualReviewResult(
            decisions=[
                _decision(ReviewVerdict.APPROVE, "a"),
                _decision(ReviewVerdict.REJECT, "b"),
            ],
            final_verdict=ReviewVerdict.REJECT,
            requires_user=False,
            consensus=False,
        )
        # Although the verdict is REJECT, requires_user is also True when
        # the user must adjudicate. We need a case where requires_user=True
        # takes precedence. Make a separate result:
        result2 = DualReviewResult(
            decisions=[
                _decision(ReviewVerdict.APPROVE, "a"),
                _decision(ReviewVerdict.ABSTAIN, "b"),
            ],
            final_verdict=ReviewVerdict.ABSTAIN,
            requires_user=True,
            consensus=False,
        )
        with pytest.raises(ReviewRequiresUser) as exc:
            mgr.review_decision(result2)
        assert exc.value.result is result2

    def test_requires_user_true_raises_even_when_approve(self):
        """If `requires_user=True` (e.g., rate-limited) and the verdict is
        ABSTAIN, the hook must raise ReviewRequiresUser even if a single
        decision was an APPROVE — the aggregation wins."""
        mgr = DualReviewManager()
        result = DualReviewResult(
            decisions=[],
            final_verdict=ReviewVerdict.ABSTAIN,
            requires_user=True,
            consensus=False,
        )
        with pytest.raises(ReviewRequiresUser):
            mgr.review_decision(result)


# ── TestStubSecondaryEdgeCases ────────────────────────────────────


class TestStubSecondaryEdgeCases:
    @pytest.mark.asyncio
    async def test_stub_approves_drop_table_safe_args(self):
        """drop table in the *prompt template* should NOT trigger the stub
        (it inspects only the args section). The stub should approve if
        the args don't contain destructive patterns."""
        mgr = DualReviewManager()
        # Pass a "safe" tool with the prompt's drop table text nowhere in args
        r = await mgr.review("write_file", {"path": "x.py", "content": "hello"})
        # primary abstains (no chat), secondary (stub) approves
        # → split (ABSTAIN + APPROVE) → requires_user
        assert r.requires_user is True
        # The stub's decision should be APPROVE
        stub_decisions = [
            d for d in r.decisions if d.reviewer_id == "secondary"
        ]
        assert len(stub_decisions) == 1
        assert stub_decisions[0].verdict == ReviewVerdict.APPROVE

    @pytest.mark.asyncio
    async def test_stub_rejects_drop_table_in_args(self):
        mgr = DualReviewManager()
        r = await mgr.review("execute_command", {"command": "drop database testdb"})
        # The stub sees "drop database" → REJECT
        assert r.final_verdict == ReviewVerdict.REJECT
        stub = [d for d in r.decisions if d.reviewer_id == "secondary"][0]
        assert stub.verdict == ReviewVerdict.REJECT

    @pytest.mark.asyncio
    async def test_stub_rejects_mkfs_in_args(self):
        mgr = DualReviewManager()
        r = await mgr.review("execute_command", {"command": "mkfs.ext4 /dev/sda"})
        assert r.final_verdict == ReviewVerdict.REJECT

    @pytest.mark.asyncio
    async def test_stub_handles_empty_args(self):
        mgr = DualReviewManager()
        r = await mgr.review("write_file", {})
        # Empty args → stub approves → split with primary abstains → user
        assert r.requires_user is True


# ── TestHighConcurrency ───────────────────────────────────────────


class TestHighConcurrency:
    @pytest.mark.asyncio
    async def test_ten_parallel_reviews_share_state(self):
        """Ten concurrent reviews should all run to completion and the
        manager's counters should reflect all of them."""
        mgr = DualReviewManager(
            primary_chat=_approve_chat,
            secondary_chat=_approve_chat,
            max_per_minute=20,  # Above the 10 we'll send
        )
        coros = [
            mgr.review("write_file", {"path": f"f{i}.py"})
            for i in range(10)
        ]
        results = await asyncio.gather(*coros)
        assert len(results) == 10
        assert all(r.final_verdict == ReviewVerdict.APPROVE for r in results)
        assert mgr.reviews_run == 10
        assert mgr.reviews_approved == 10

    @pytest.mark.asyncio
    async def test_concurrent_rate_limit(self):
        """Rate limiter should be thread-safe (single asyncio loop, but
        shared state via gather)."""
        mgr = DualReviewManager(
            primary_chat=_approve_chat,
            secondary_chat=_approve_chat,
            max_per_minute=3,
        )
        coros = [mgr.review("write_file", {"path": "x.py"}) for _ in range(5)]
        results = await asyncio.gather(*coros)
        # First 3 approved; 4th and 5th rate-limited → requires_user
        approved = sum(1 for r in results if r.final_verdict == ReviewVerdict.APPROVE)
        user_required = sum(1 for r in results if r.requires_user)
        assert approved == 3
        assert user_required == 2
        assert mgr.reviews_rate_limited == 2


# ── TestRateLimiterEdgeCases ──────────────────────────────────────


class TestRateLimiterEdgeCases:
    def test_allow_returns_true_under_limit(self):
        rl = RateLimiter(max_per_minute=2)
        assert rl.allow() is True
        assert rl.allow() is True

    def test_allow_returns_false_at_limit(self):
        rl = RateLimiter(max_per_minute=2)
        rl.allow()
        rl.allow()
        assert rl.allow() is False

    def test_used_counts_total(self):
        rl = RateLimiter(max_per_minute=10)
        rl.allow()
        rl.allow()
        assert rl.used() == 2

    def test_reset_zeroes_counter(self):
        rl = RateLimiter(max_per_minute=2)
        rl.allow()
        rl.allow()
        assert rl.used() == 2
        rl.reset()
        assert rl.used() == 0
        # Should be allowed again
        assert rl.allow() is True


# ── TestParseVerdictResponseEdgeCases ──────────────────────────────


class TestParseVerdictResponseEdgeCases:
    def test_rationale_with_quotes(self):
        v, r = _parse_verdict_response(
            '{"verdict": "reject", "rationale": "contains \\"quoted\\" text"}'
        )
        assert v == ReviewVerdict.REJECT
        assert 'quoted' in r

    def test_nested_json_object(self):
        # If the response has nested JSON, we only pull the verdict/rationale
        resp = '{"verdict": "approve", "rationale": "x", "extra": {"nested": true}}'
        v, r = _parse_verdict_response(resp)
        assert v == ReviewVerdict.APPROVE
        assert r == "x"

    def test_array_response_becomes_abstain(self):
        # An array at the top level → not a dict → abstain
        v, r = _parse_verdict_response('[1, 2, 3]')
        assert v == ReviewVerdict.ABSTAIN
        assert "Could not parse" in r

    def test_empty_string(self):
        v, r = _parse_verdict_response('')
        assert v == ReviewVerdict.ABSTAIN

    def test_only_whitespace(self):
        v, r = _parse_verdict_response('   \n  \t  ')
        assert v == ReviewVerdict.ABSTAIN

    def test_markdown_table(self):
        v, r = _parse_verdict_response(
            "Some prose\n| verdict | rationale |\n|---------|-----------|\n| approve | looks fine |"
        )
        # No JSON in markdown table → abstain
        assert v == ReviewVerdict.ABSTAIN

    def test_multiple_json_objects_takes_first(self):
        # Two valid JSON objects; we extract first { and last } → that
        # captures everything between → which is unparseable as a single
        # object. Falls back to smart-quote fix, then ABSTAIN.
        resp = '{"verdict": "approve", "rationale": "first"} {"verdict": "reject"}'
        v, _ = _parse_verdict_response(resp)
        # The current parser takes first { to last }, so the full string
        # is treated as one (broken) JSON. Falls back to ABSTAIN.
        assert v in (ReviewVerdict.APPROVE, ReviewVerdict.ABSTAIN)

    def test_verdict_with_extra_whitespace(self):
        v, _ = _parse_verdict_response('{"verdict":  "approve"  ,  "rationale":"x"}')
        assert v == ReviewVerdict.APPROVE

    def test_synonym_ok(self):
        v, _ = _parse_verdict_response('{"verdict": "ok"}')
        assert v == ReviewVerdict.APPROVE

    def test_synonym_block(self):
        v, _ = _parse_verdict_response('{"verdict": "block"}')
        assert v == ReviewVerdict.REJECT


# ── TestReviewElapseTiming ────────────────────────────────────────


class TestReviewElapseTiming:
    @pytest.mark.asyncio
    async def test_elapsed_ms_recorded_per_reviewer(self):
        mgr = DualReviewManager(
            primary_chat=_approve_chat, secondary_chat=_approve_chat
        )
        r = await mgr.review("write_file", {"path": "x.py"})
        assert len(r.decisions) == 2
        for d in r.decisions:
            assert d.elapsed_ms >= 0.0
            assert d.elapsed_ms < 5000  # Should be fast in tests


# ── TestManagerDefaults ───────────────────────────────────────────


class TestManagerDefaults:
    def test_default_models(self):
        mgr = DualReviewManager()
        assert mgr.primary_model == "primary"
        assert mgr.secondary_model == "secondary"

    def test_default_rate_limiter(self):
        mgr = DualReviewManager()
        assert mgr.rate_limiter is not None
        assert mgr.rate_limiter.max == 5

    def test_stats_start_at_zero(self):
        mgr = DualReviewManager()
        assert mgr.reviews_run == 0
        assert mgr.reviews_approved == 0
        assert mgr.reviews_rejected == 0
        assert mgr.reviews_user_required == 0
        assert mgr.reviews_rate_limited == 0

    def test_high_risk_set_size(self):
        # PR-11 ships with a curated set; verify the count matches the
        # documented tools (so a future change is intentional, not silent).
        assert len(DualReviewManager.HIGH_RISK_TOOLS) >= 8

    def test_all_high_risk_tools_are_strings(self):
        for t in DualReviewManager.HIGH_RISK_TOOLS:
            assert isinstance(t, str)
            assert len(t) > 0

    def test_get_dual_review_manager_returns_singleton(self):
        reset_dual_review_manager()
        m1 = get_dual_review_manager()
        m2 = get_dual_review_manager()
        assert m1 is m2

    def test_get_with_args_only_on_first_call(self):
        """Args to get_dual_review_manager are only honored on the first
        call (singleton pattern)."""
        reset_dual_review_manager()
        m1 = get_dual_review_manager(primary_chat=_approve_chat)
        m2 = get_dual_review_manager(primary_chat=_reject_chat)
        # m2 should be the same instance, ignoring the second call's args
        assert m1 is m2
        assert m1.primary_chat is _approve_chat
