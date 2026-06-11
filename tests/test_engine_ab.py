"""Engine integration tests for A/B testing (PR-12).

Verifies:
- The engine initializes an ABTestManager singleton
- BEFORE_LLM_CALL hook applies running variants
- ON_SESSION_END hook writes observations
- _resolve_ab_user_id honors config + env + workspace
- A/B is opt-out via AgentConfig.ab_test_enabled
"""

import json
import time

import pytest

from agent.core.audit_log import reset_audit_logger
from agent.core.engine import AgentConfig, AgentEngine
from agent.core.hooks import BEFORE_LLM_CALL
from agent.governance.ab_test import (
    ABTestManager,
    Experiment,
    ExperimentStatus,
    ExperimentVariant,
    reset_ab_test_manager,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _config(**overrides) -> AgentConfig:
    base = dict(
        model="mock",
        provider="mock",
        tdd_mode="off",
        ab_test_enabled=True,
        ab_user_id="alice",
    )
    base.update(overrides)
    return AgentConfig(**base)


def _make_exp(
    exp_id: str = "exp_test", target="system_prompt", target_key="INTRO", min_samples=5, **overrides
) -> Experiment:
    return Experiment(
        id=exp_id,
        name=overrides.get("name", f"Test {exp_id}"),
        description=overrides.get("description", "test"),
        target=target,
        target_key=target_key,
        variants=overrides.get(
            "variants",
            [
                ExperimentVariant(
                    id="A", name="control", config={"new_content": "You are a control agent."}
                ),
                ExperimentVariant(
                    id="B", name="treatment", config={"new_content": "You are a treatment agent."}
                ),
            ],
        ),
        min_samples=min_samples,
    )


# ── TestEngineWiring ───────────────────────────────────────────────


class TestEngineWiring:
    def test_ab_test_default_enabled(self):
        e = AgentEngine(_config())
        assert e.ab_test is not None
        assert isinstance(e.ab_test, ABTestManager)

    def test_ab_test_disabled_via_config(self):
        e = AgentEngine(_config(ab_test_enabled=False))
        assert e.ab_test is None

    def test_hooks_registered(self):
        e = AgentEngine(_config())
        # BEFORE_LLM_CALL should be registered (multiple hooks use it)
        assert e.hooks.has(BEFORE_LLM_CALL)
        # ON_SESSION_END gets registered via the run_stream loop; here we
        # check the method exists on the engine
        assert callable(e._ab_apply_variants_hook)
        assert callable(e._ab_record_observation_hook)

    def test_user_id_uses_config(self):
        e = AgentEngine(_config(ab_user_id="bob"))
        assert e._ab_user_id == "bob"

    def test_user_id_empty_config_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("USER", "charlie")
        e = AgentEngine(_config(ab_user_id=""))
        assert e._ab_user_id == "charlie"

    def test_user_id_falls_back_to_workspace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        # Default workspace is derived from the env or config
        e = AgentEngine(_config(ab_user_id=""))
        # Should at least be a non-empty string
        assert e._ab_user_id
        assert e._ab_user_id != ""


# ── TestApplyVariantsHook ──────────────────────────────────────────


class TestApplyVariantsHook:
    @pytest.fixture(autouse=True)
    def _reset_ab(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        reset_ab_test_manager()
        reset_audit_logger()
        yield
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_no_active_experiments(self):
        e = AgentEngine(_config())
        payload = {"system": "You are an agent. INTRO is here.", "messages": []}
        out = await e._ab_apply_variants_hook(payload)
        assert out is payload
        # system prompt should be unchanged
        assert out["system"] == "You are an agent. INTRO is here."

    @pytest.mark.asyncio
    async def test_applies_variant_a(self):
        e = AgentEngine(_config())
        exp = _make_exp("apply_a", target_key="INTRO")
        e.ab_test.create(exp)
        payload = {"system": "You are an agent. INTRO is here.", "messages": []}
        out = await e._ab_apply_variants_hook(payload)
        # The system prompt should be modified (either A or B's content
        # depending on hash bucketing — we just verify *some* variant
        # was applied and the marker was replaced).
        assert "INTRO" not in out["system"]
        assert (
            "You are a control agent." in out["system"]
            or "You are a treatment agent." in out["system"]
        )
        # In-flight list tracks this experiment
        assert any(x["experiment_id"] == "apply_a" for x in out["_ab_experiments"])
        # And the variant_id is recorded
        entry = next(x for x in out["_ab_experiments"] if x["experiment_id"] == "apply_a")
        assert entry["variant_id"] in ("A", "B")

    @pytest.mark.asyncio
    async def test_non_dict_payload_ignored(self):
        e = AgentEngine(_config())
        result = await e._ab_apply_variants_hook("not a dict")
        assert result == "not a dict"

    @pytest.mark.asyncio
    async def test_no_system_prompt(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("nosp"))
        payload = {"messages": []}
        out = await e._ab_apply_variants_hook(payload)
        # Nothing to do; no _ab_experiments key set
        assert "_ab_experiments" not in out

    @pytest.mark.asyncio
    async def test_skips_completed_experiments(self):
        e = AgentEngine(_config())
        exp = _make_exp("done", target_key="INTRO")
        e.ab_test.create(exp)
        e.ab_test._cache["done"].status = ExperimentStatus.COMPLETED.value
        payload = {"system": "INTRO marker here."}
        out = await e._ab_apply_variants_hook(payload)
        # Should NOT replace because experiment is not running
        assert "INTRO marker here." == out["system"]

    @pytest.mark.asyncio
    async def test_skips_non_system_prompt_target(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("tool_target", target="tool_default", target_key="INTRO"))
        payload = {"system": "INTRO marker here."}
        out = await e._ab_apply_variants_hook(payload)
        # tool_default is not applied to system prompt
        assert "INTRO marker here." == out["system"]

    @pytest.mark.asyncio
    async def test_marker_not_in_prompt(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("missing", target_key="NONEXISTENT"))
        payload = {"system": "Some prompt without the marker."}
        out = await e._ab_apply_variants_hook(payload)
        # No replacement, but experiment is still recorded
        assert any(x["experiment_id"] == "missing" for x in out["_ab_experiments"])

    @pytest.mark.asyncio
    async def test_recorded_only_once_per_call(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("once", target_key="INTRO"))
        payload = {"system": "INTRO marker."}
        out = await e._ab_apply_variants_hook(payload)
        ids = [
            x["experiment_id"] for x in out["_ab_experiments"] if x.get("experiment_id") == "once"
        ]
        assert len(ids) == 1


# ── TestRecordObservationHook ─────────────────────────────────────


class TestRecordObservationHook:
    @pytest.fixture(autouse=True)
    def _reset_ab(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        reset_ab_test_manager()
        reset_audit_logger()
        yield
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_no_in_flight_no_record(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("norec"))
        payload = {"final_state": {"trace_id": "t"}, "result": None}
        await e._ab_record_observation_hook(payload)
        # Observations file should be empty or absent
        log = e.ab_test.exp_dir / "norec" / "observations.jsonl"
        if log.exists():
            assert log.read_text().strip() == ""

    @pytest.mark.asyncio
    async def test_records_one_observation_per_experiment(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("rec1", target_key="INTRO"))
        e.ab_test.create(_make_exp("rec2", target_key="OTHER"))
        e._ab_last_task = "test task"
        e._ab_task_start_ts = None
        # Simulate apply hook having run and set in_flight
        payload = {
            "final_state": {"trace_id": "t"},
            "result": None,
            "_ab_experiments": [
                {"experiment_id": "rec1", "variant_id": "A", "variant_name": "control"},
                {"experiment_id": "rec2", "variant_id": "B", "variant_name": "treatment"},
            ],
        }
        await e._ab_record_observation_hook(payload)
        # Each experiment has its own observations file
        for exp_id in ("rec1", "rec2"):
            log = e.ab_test.exp_dir / exp_id / "observations.jsonl"
            assert log.exists()
            lines = log.read_text().strip().split("\n")
            assert len(lines) == 1
            rec = json.loads(lines[0])
            assert rec["experiment_id"] == exp_id
            assert rec["user_id"] == "alice"
            assert rec["task"] == "test task"
            assert rec["success"] is True

    @pytest.mark.asyncio
    async def test_records_failure(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("fail", target_key="INTRO"))
        e._ab_last_task = "failing task"
        payload = {
            "final_state": {"trace_id": "t"},
            "result": None,
            "error": "Tool X failed",
            "_ab_experiments": [
                {"experiment_id": "fail", "variant_id": "A", "variant_name": "control"},
            ],
        }
        await e._ab_record_observation_hook(payload)
        log = e.ab_test.exp_dir / "fail" / "observations.jsonl"
        rec = json.loads(log.read_text().strip())
        assert rec["success"] is False

    @pytest.mark.asyncio
    async def test_computes_duration(self):
        import time

        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("dur", target_key="INTRO"))
        e._ab_task_start_ts = time.time() - 1.0  # 1 second ago
        e._ab_last_task = "t"
        payload = {
            "final_state": {"trace_id": "t"},
            "result": None,
            "_ab_experiments": [
                {"experiment_id": "dur", "variant_id": "A", "variant_name": "control"},
            ],
        }
        await e._ab_record_observation_hook(payload)
        log = e.ab_test.exp_dir / "dur" / "observations.jsonl"
        rec = json.loads(log.read_text().strip())
        # duration_ms should be > 0 (we waited 1 second)
        assert rec["duration_ms"] > 500.0

    @pytest.mark.asyncio
    async def test_records_token_counts(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("tok", target_key="INTRO"))
        e._ab_last_task = "t"
        e._total_input_tokens = 42
        e._total_output_tokens = 17
        payload = {
            "final_state": {"trace_id": "t"},
            "result": None,
            "_ab_experiments": [
                {"experiment_id": "tok", "variant_id": "A", "variant_name": "control"},
            ],
        }
        await e._ab_record_observation_hook(payload)
        log = e.ab_test.exp_dir / "tok" / "observations.jsonl"
        rec = json.loads(log.read_text().strip())
        assert rec["token_input"] == 42
        assert rec["token_output"] == 17

    @pytest.mark.asyncio
    async def test_handles_non_dict(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("hnd"))
        result = await e._ab_record_observation_hook(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_malformed_in_flight(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("mal"))
        # In-flight entry missing fields — should not raise
        payload = {
            "final_state": {"trace_id": "t"},
            "result": None,
            "_ab_experiments": [
                {},  # missing experiment_id / variant_id
            ],
        }
        # Should not raise
        await e._ab_record_observation_hook(payload)


# ── TestDisabledEngineSkipsAB ─────────────────────────────────────


class TestDisabledEngineSkipsAB:
    def test_no_manager(self):
        e = AgentEngine(_config(ab_test_enabled=False))
        assert e.ab_test is None

    @pytest.mark.asyncio
    async def test_apply_hook_returns_payload_unchanged(self):
        e = AgentEngine(_config(ab_test_enabled=False))
        payload = {"system": "INTRO marker here."}
        out = await e._ab_apply_variants_hook(payload)
        assert out is payload

    @pytest.mark.asyncio
    async def test_record_hook_returns_payload_unchanged(self):
        e = AgentEngine(_config(ab_test_enabled=False))
        payload = {"_ab_experiments": [{"experiment_id": "x", "variant_id": "A"}]}
        out = await e._ab_record_observation_hook(payload)
        assert out is payload


# ── TestEndToEndApplyThenRecord ──────────────────────────────────


class TestEndToEndApplyThenRecord:
    @pytest.fixture(autouse=True)
    def _reset_ab(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path / "exp"))
        reset_ab_test_manager()
        reset_audit_logger()
        yield
        reset_ab_test_manager()
        reset_audit_logger()

    @pytest.mark.asyncio
    async def test_full_flow(self):
        e = AgentEngine(_config())
        e.ab_test.create(_make_exp("e2e", target_key="INTRO", min_samples=5))
        # Phase 1: apply variants (LLM call)
        payload = {"system": "You are an agent. INTRO is here.", "messages": []}
        payload = await e._ab_apply_variants_hook(payload)
        assert "INTRO" not in payload["system"]
        # Phase 2: session ends → record observation
        e._ab_last_task = "real task"
        e._total_input_tokens = 100
        e._total_output_tokens = 50
        e._ab_task_start_ts = None
        end_payload = dict(payload)
        end_payload["final_state"] = {"trace_id": "t"}
        end_payload["result"] = "ok"
        await e._ab_record_observation_hook(end_payload)
        log = e.ab_test.exp_dir / "e2e" / "observations.jsonl"
        assert log.exists()
        rec = json.loads(log.read_text().strip())
        assert rec["experiment_id"] == "e2e"
        assert rec["success"] is True
        assert rec["token_input"] == 100
        assert rec["token_output"] == 50


# ── TestUserIdResolution ──────────────────────────────────────────


class TestUserIdResolution:
    def test_explicit_config(self):
        e = AgentEngine(_config(ab_user_id="explicit_user"))
        assert e._ab_user_id == "explicit_user"

    def test_user_env_var(self, monkeypatch):
        monkeypatch.setenv("USER", "env_user")
        e = AgentEngine(_config(ab_user_id=""))
        assert e._ab_user_id == "env_user"

    def test_username_env_var(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.setenv("USERNAME", "win_user")
        e = AgentEngine(_config(ab_user_id=""))
        assert e._ab_user_id == "win_user"

    def test_workspace_fallback(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        e = AgentEngine(_config(ab_user_id=""))
        # Falls back to workspace path or "anonymous"
        assert e._ab_user_id
        assert e._ab_user_id != ""


# ── TestSingletonSharedAcrossEngines ─────────────────────────────


class TestSingletonSharedAcrossEngines:
    def test_same_singleton(self):
        # Reset to ensure clean state
        reset_ab_test_manager()
        e1 = AgentEngine(_config())
        e2 = AgentEngine(_config())
        # Singleton is shared, so both engines reference the same object
        assert e1.ab_test is e2.ab_test


# ── TestABTestDisabled_Regression ─────────────────────────────────


class TestABTestDisabledRegression:
    """The engine should still work normally with AB disabled."""

    def test_engine_works_without_ab(self):
        e = AgentEngine(_config(ab_test_enabled=False))
        assert e.ab_test is None
        # Other engine features should still work
        assert e.event_bus is not None
        assert e.hooks is not None


# ── TestMultipleExperimentsInFlight ─────────────────────────────


class TestMultipleExperimentsInFlight:
    """Test that the engine tracks all active experiments, not just one."""

    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        from agent.governance.ab_test import reset_ab_test_manager

        reset_ab_test_manager()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path))
        yield
        reset_ab_test_manager()

    @pytest.mark.asyncio
    async def test_tracks_two_experiments(self, monkeypatch):
        e = AgentEngine(_config(ab_user_id="alice"))
        # Two experiments
        e.ab_test.create(
            _make_exp(
                "exp_a",
                target_key="marker_a",
                variants=[
                    ExperimentVariant(id="A", name="c1", config={"new_content": "v_a"}),
                    ExperimentVariant(id="B", name="t1", config={"new_content": "v_b"}),
                ],
            )
        )
        e.ab_test.create(
            _make_exp(
                "exp_b",
                target_key="marker_b",
                variants=[
                    ExperimentVariant(id="A", name="c2", config={"new_content": "v_c"}),
                    ExperimentVariant(id="B", name="t2", config={"new_content": "v_d"}),
                ],
            )
        )
        payload = {
            "system": "before\nmarker_a\n---\nbefore\nmarker_b\n---",
            "messages": [],
        }
        out = await e._ab_apply_variants_hook(payload)
        in_flight = out.get("_ab_experiments", [])
        # Two experiments tracked
        assert len(in_flight) == 2
        exp_ids = {e["experiment_id"] for e in in_flight}
        assert exp_ids == {"exp_a", "exp_b"}
        # Both markers replaced
        assert "marker_a" not in out["system"]
        assert "marker_b" not in out["system"]

    @pytest.mark.asyncio
    async def test_no_markers_in_flight_still_tracked(self, monkeypatch):
        """If the system prompt doesn't contain the marker, the
        experiment is STILL added to in_flight — the marker check only
        gates the actual replacement, not the tracking. This way every
        running system_prompt experiment gets an observation per session,
        even if its specific marker wasn't present."""
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(
            _make_exp(
                "exp_x",
                target_key="marker_x",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": "new"}),
                    ExperimentVariant(id="B", name="t", config={"new_content": "new"}),
                ],
            )
        )
        # System prompt has no marker
        payload = {"system": "no marker here", "messages": []}
        out = await e._ab_apply_variants_hook(payload)
        # Experiment IS still tracked (for observation purposes)
        in_flight = out.get("_ab_experiments", [])
        assert len(in_flight) == 1
        # But the system prompt is unchanged (marker not replaced)
        assert "marker_x" not in out["system"]


# ── TestABRecordFailureRecovery ─────────────────────────────────


class TestABRecordFailureRecovery:
    """Test that record_observation gracefully handles edge cases."""

    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        from agent.governance.ab_test import reset_ab_test_manager

        reset_ab_test_manager()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path))
        yield
        reset_ab_test_manager()

    @pytest.mark.asyncio
    async def test_in_flight_with_missing_fields_does_not_crash(self):
        """If in_flight entries are missing variant_id or experiment_id,
        the record hook should skip them, not crash."""
        e = AgentEngine(_config(ab_user_id="alice"))
        # _ab_experiments exists but with a broken entry
        payload = {
            "_ab_experiments": [
                {"experiment_id": "exp_x", "variant_id": "A"},
                # Missing fields
                {},
            ],
        }
        # Should not raise
        out = await e._ab_record_observation_hook(payload)
        assert out is payload  # unchanged

    @pytest.mark.asyncio
    async def test_in_flight_with_unknown_experiment_does_not_crash(self):
        e = AgentEngine(_config(ab_user_id="alice"))
        payload = {
            "_ab_experiments": [
                {"experiment_id": "unknown_exp", "variant_id": "A"},
            ],
        }
        # Should not raise (observation is silently skipped)
        out = await e._ab_record_observation_hook(payload)
        assert out is payload

    @pytest.mark.asyncio
    async def test_records_zero_duration_when_start_ts_missing(self):
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(_make_exp("exp_dur", min_samples=1))
        e._ab_task_start_ts = None
        payload = {
            "_ab_experiments": [
                {"experiment_id": "exp_dur", "variant_id": "A"},
            ],
            "error": None,
        }
        await e._ab_record_observation_hook(payload)
        obs = e.ab_test.observations("exp_dur")
        assert len(obs) == 1
        assert obs[0].duration_ms == 0.0

    @pytest.mark.asyncio
    async def test_records_token_counts_from_engine_totals(self):
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(_make_exp("exp_tok", min_samples=1))
        e._total_input_tokens = 1234
        e._total_output_tokens = 567
        payload = {
            "_ab_experiments": [
                {"experiment_id": "exp_tok", "variant_id": "A"},
            ],
        }
        await e._ab_record_observation_hook(payload)
        obs = e.ab_test.observations("exp_tok")
        assert obs[0].token_input == 1234
        assert obs[0].token_output == 567


# ── TestABApplyVariantsEdgeCases ────────────────────────────────


class TestABApplyVariantsEdgeCases:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        from agent.governance.ab_test import reset_ab_test_manager

        reset_ab_test_manager()
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path))
        yield
        reset_ab_test_manager()

    @pytest.mark.asyncio
    async def test_multiple_marker_occurrences_replaces_first(self):
        """Marker should be replaced only once (replace count=1) to
        prevent runaway substitutions."""
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(
            _make_exp(
                "exp_rep",
                target_key="marker",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": "X"}),
                    ExperimentVariant(id="B", name="t", config={"new_content": "X"}),
                ],
            )
        )
        payload = {"system": "marker one marker two", "messages": []}
        out = await e._ab_apply_variants_hook(payload)
        # First occurrence replaced; second left alone
        assert "X one marker two" in out["system"]

    @pytest.mark.asyncio
    async def test_empty_replacement_does_not_substitute(self):
        """If the variant's new_content is empty, no replacement happens."""
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(
            _make_exp(
                "exp_empty",
                target_key="marker",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": ""}),
                    ExperimentVariant(id="B", name="t", config={"new_content": ""}),
                ],
            )
        )
        original = "marker is here"
        payload = {"system": original, "messages": []}
        out = await e._ab_apply_variants_hook(payload)
        # No replacement
        assert out["system"] == original
        # But the experiment IS still tracked (so we get a "control" observation)
        assert len(out.get("_ab_experiments", [])) == 1

    @pytest.mark.asyncio
    async def test_apply_then_record_in_full_cycle(self):
        """Full cycle: apply variant, then record observation."""
        e = AgentEngine(_config(ab_user_id="alice"))
        e.ab_test.create(
            _make_exp(
                "exp_full",
                variants=[
                    ExperimentVariant(id="A", name="c", config={"new_content": "control"}),
                    ExperimentVariant(id="B", name="t", config={"new_content": "treatment"}),
                ],
            )
        )
        e._ab_task_start_ts = time.time()
        e._ab_last_task = "do something"
        e._total_input_tokens = 100
        e._total_output_tokens = 50
        # Apply phase
        payload = {"system": "marker", "messages": []}
        payload = await e._ab_apply_variants_hook(payload)
        # Record phase
        record_payload = {
            "_ab_experiments": payload["_ab_experiments"],
            "error": None,
        }
        await e._ab_record_observation_hook(record_payload)
        obs = e.ab_test.observations("exp_full")
        assert len(obs) == 1
        # The user got whatever variant was assigned
        assert obs[0].variant_id in {"A", "B"}
        # The user_id was captured
        assert obs[0].user_id == "alice"
        # Token counts
        assert obs[0].token_input == 100
        assert obs[0].token_output == 50
