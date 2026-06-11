"""Unit tests for A/B testing framework (PR-12)."""

import json
import pytest
from pathlib import Path

from agent.governance.ab_test import (
    ABTestManager,
    Experiment,
    ExperimentAnalysis,
    ExperimentObservation,
    ExperimentStatus,
    ExperimentTarget,
    ExperimentVariant,
    get_ab_test_manager,
    reset_ab_test_manager,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_exp(exp_id: str = "exp_test", **overrides) -> Experiment:
    """Build a 2-variant experiment with sensible defaults."""
    exp = Experiment(
        id=exp_id,
        name=overrides.get("name", f"Test {exp_id}"),
        description=overrides.get("description", "test experiment"),
        target=overrides.get("target", ExperimentTarget.SYSTEM_PROMPT.value),
        target_key=overrides.get("target_key", "intro"),
        variants=overrides.get("variants", [
            ExperimentVariant(id="A", name="control",
                              config={"new_content": "You are a control agent."}),
            ExperimentVariant(id="B", name="treatment",
                              config={"new_content": "You are a treatment agent."}),
        ]),
        min_samples=overrides.get("min_samples", 5),
    )
    return exp


def _make_obs(exp_id: str, variant: str, success: bool, n: int = 1,
              tokens: int = 100, duration: float = 50.0,
              user_id: str = "alice", rating: int = None) -> list:
    """Build N identical observations for one variant."""
    return [
        ExperimentObservation(
            experiment_id=exp_id,
            variant_id=variant,
            user_id=user_id,
            task=f"task_{i}",
            success=success,
            token_input=tokens // 2,
            token_output=tokens // 2,
            duration_ms=duration,
            user_rating=rating,
        )
        for i in range(n)
    ]


# ── TestVariantDataClass ───────────────────────────────────────────


class TestVariantDataClass:
    def test_round_trip(self):
        v = ExperimentVariant(id="A", name="control", config={"x": 1}, weight=0.7)
        d = v.to_dict()
        v2 = ExperimentVariant.from_dict(d)
        assert v2.id == "A"
        assert v2.name == "control"
        assert v2.config == {"x": 1}
        assert v2.weight == 0.7

    def test_default_weight(self):
        v = ExperimentVariant(id="A", name="a", config={})
        assert v.weight == 1.0


# ── TestExperimentDataClass ────────────────────────────────────────


class TestExperimentDataClass:
    def test_round_trip(self):
        e = _make_exp()
        d = e.to_dict()
        e2 = Experiment.from_dict(d)
        assert e2.id == e.id
        assert e2.name == e.name
        assert e2.target == e.target
        assert e2.target_key == e.target_key
        assert len(e2.variants) == 2
        assert e2.min_samples == 5

    def test_variant_by_id(self):
        e = _make_exp()
        v = e.variant_by_id("A")
        assert v is not None
        assert v.name == "control"
        assert e.variant_by_id("Z") is None


# ── TestObservationDataClass ───────────────────────────────────────


class TestObservationDataClass:
    def test_round_trip(self):
        o = ExperimentObservation(
            experiment_id="e1", variant_id="A", user_id="u",
            task="t", success=True, token_input=10, token_output=20,
            duration_ms=100.0, user_rating=4,
        )
        d = o.to_dict()
        o2 = ExperimentObservation.from_dict(d)
        assert o2.experiment_id == "e1"
        assert o2.user_rating == 4

    def test_round_trip_no_rating(self):
        o = ExperimentObservation(
            experiment_id="e1", variant_id="A", user_id="u",
            task="t", success=True, token_input=10, token_output=20,
            duration_ms=100.0,
        )
        d = o.to_dict()
        o2 = ExperimentObservation.from_dict(d)
        assert o2.user_rating is None

    def test_default_ts(self):
        o = ExperimentObservation(
            experiment_id="e1", variant_id="A", user_id="u", task="t",
            success=True, token_input=0, token_output=0, duration_ms=0.0,
        )
        assert o.ts == ""


# ── TestManagerCreate ──────────────────────────────────────────────


class TestManagerCreate:
    def test_create_persists_file(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("exp_persist")
        mgr.create(e)
        f = tmp_path / "exp_persist" / "experiment.json"
        assert f.exists()
        loaded = json.loads(f.read_text())
        assert loaded["id"] == "exp_persist"
        assert len(loaded["variants"]) == 2

    def test_create_assigns_id_if_missing(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("")  # empty id
        e.id = ""
        mgr.create(e)
        assert e.id.startswith("exp_")
        assert len(e.id) > 5

    def test_create_assigns_variant_ids(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        v1 = ExperimentVariant(id="", name="c", config={})
        v2 = ExperimentVariant(id="", name="t", config={})
        e = Experiment(
            id="x", name="x", description="x",
            target=ExperimentTarget.SYSTEM_PROMPT.value,
            target_key="k",
            variants=[v1, v2],
        )
        mgr.create(e)
        ids = [v.id for v in e.variants]
        assert ids == ["A", "B"]

    def test_create_rejects_too_few_variants(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("solo")
        e.variants = [ExperimentVariant(id="A", name="only", config={})]
        with pytest.raises(ValueError, match="at least 2"):
            mgr.create(e)

    def test_create_rejects_duplicate_variant_ids(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("dup")
        e.variants = [
            ExperimentVariant(id="A", name="a", config={}),
            ExperimentVariant(id="A", name="b", config={}),
        ]
        with pytest.raises(ValueError, match="unique"):
            mgr.create(e)

    def test_create_sets_timestamps(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("ts")
        mgr.create(e)
        assert e.created_at != ""
        assert e.started_at == e.created_at


# ── TestManagerListAndGet ──────────────────────────────────────────


class TestManagerListAndGet:
    def test_list_empty(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        assert mgr.list() == []

    def test_create_then_list(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("a"))
        mgr.create(_make_exp("b"))
        assert len(mgr.list()) == 2

    def test_get_existing(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("hello"))
        assert mgr.get("hello") is not None

    def test_get_missing(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        assert mgr.get("nonexistent") is None

    def test_load_all_recovers_existing(self, tmp_path):
        mgr1 = ABTestManager(exp_dir=tmp_path)
        mgr1.create(_make_exp("recov"))
        # New manager reads the same dir
        mgr2 = ABTestManager(exp_dir=tmp_path)
        assert mgr2.get("recov") is not None


# ── TestAssignVariant ──────────────────────────────────────────────


class TestAssignVariant:
    def test_deterministic_per_user(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("bucket"))
        v1 = mgr.assign_variant("bucket", "alice")
        v2 = mgr.assign_variant("bucket", "alice")
        assert v1.id == v2.id

    def test_different_users_may_get_different_variants(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("spread"))
        # Across 100 users, both A and B should appear
        ids = {mgr.assign_variant("spread", f"user_{i}").id for i in range(100)}
        assert "A" in ids and "B" in ids

    def test_returns_none_for_unknown_experiment(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        assert mgr.assign_variant("nope", "alice") is None

    def test_completed_experiment_returns_winner(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("ended"))
        # Mark completed
        mgr._cache["ended"].status = ExperimentStatus.COMPLETED.value
        mgr._cache["ended"].winner = "B"
        v = mgr.assign_variant("ended", "alice")
        assert v.id == "B"

    def test_completed_no_winner_returns_first_variant(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("no_winner"))
        mgr._cache["no_winner"].status = ExperimentStatus.COMPLETED.value
        mgr._cache["no_winner"].winner = ""
        v = mgr.assign_variant("no_winner", "alice")
        assert v.id == "A"

    def test_abandoned_returns_first_variant(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("aband"))
        mgr.abandon("aband")
        v = mgr.assign_variant("aband", "alice")
        assert v.id == "A"

    def test_distribution_is_reasonably_balanced(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("dist"))
        # 1000 users, uniform 50/50 → both buckets should be ~500 (±100)
        counts = {"A": 0, "B": 0}
        for i in range(1000):
            v = mgr.assign_variant("dist", f"user_{i}")
            counts[v.id] += 1
        assert 400 <= counts["A"] <= 600
        assert 400 <= counts["B"] <= 600

    def test_weighted_distribution(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        e = _make_exp("weighted")
        e.variants = [
            ExperimentVariant(id="A", name="a", config={}, weight=0.2),
            ExperimentVariant(id="B", name="b", config={}, weight=0.8),
        ]
        mgr.create(e)
        counts = {"A": 0, "B": 0}
        for i in range(2000):
            v = mgr.assign_variant("weighted", f"u{i}")
            counts[v.id] += 1
        # B should get roughly 4× A's traffic
        assert counts["B"] > counts["A"] * 2


# ── TestRecordObservation ──────────────────────────────────────────


class TestRecordObservation:
    def test_appends_to_file(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("obs"))
        obs = _make_obs("obs", "A", True)[0]
        mgr.record_observation(obs)
        log = tmp_path / "obs" / "observations.jsonl"
        assert log.exists()
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["experiment_id"] == "obs"
        assert rec["variant_id"] == "A"

    def test_appends_multiple(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("multi"))
        for o in _make_obs("multi", "A", True, n=5):
            mgr.record_observation(o)
        log = tmp_path / "multi" / "observations.jsonl"
        assert len(log.read_text().strip().split("\n")) == 5

    def test_sets_timestamp(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("ts_obs"))
        obs = _make_obs("ts_obs", "A", True)[0]
        assert obs.ts == ""
        mgr.record_observation(obs)
        assert obs.ts != ""


# ── TestObservations ───────────────────────────────────────────────


class TestObservations:
    def test_empty(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("empty"))
        assert mgr.observations("empty") == []

    def test_round_trip(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("rt"))
        for o in _make_obs("rt", "A", True, n=3):
            mgr.record_observation(o)
        obs_list = mgr.observations("rt")
        assert len(obs_list) == 3
        assert all(isinstance(o, ExperimentObservation) for o in obs_list)

    def test_tolerates_malformed_lines(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("malformed"))
        log = tmp_path / "malformed" / "observations.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            '{"experiment_id":"malformed","variant_id":"A","user_id":"u","task":"t","success":true,"token_input":0,"token_output":0,"duration_ms":0.0,"ts":""}\n'
            "this is not json\n"
            "{broken\n"
        )
        obs_list = mgr.observations("malformed")
        assert len(obs_list) == 1


# ── TestAnalyzeNoData ──────────────────────────────────────────────


class TestAnalyzeNoData:
    def test_no_observations(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("none"))
        a = mgr.analyze("none")
        assert a.status == "no_data"

    def test_unknown_experiment(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        a = mgr.analyze("missing")
        assert a.status == "not_found"


# ── TestAnalyzeInsufficientSamples ─────────────────────────────────


class TestAnalyzeInsufficientSamples:
    def test_under_min(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("under", min_samples=10))
        for o in _make_obs("under", "A", True, n=5):
            mgr.record_observation(o)
        for o in _make_obs("under", "B", True, n=5):
            mgr.record_observation(o)
        a = mgr.analyze("under")
        assert a.status == "insufficient_samples"
        assert a.have == {"A": 5, "B": 5}

    def test_one_variant_only(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("onevar"))
        for o in _make_obs("onevar", "A", True, n=10):
            mgr.record_observation(o)
        a = mgr.analyze("onevar")
        assert a.status == "insufficient_variants"


# ── TestAnalyzeWinner ──────────────────────────────────────────────


class TestAnalyzeWinner:
    def test_b_wins_clearly(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("b_win", min_samples=5))
        for o in _make_obs("b_win", "A", True, n=10):
            mgr.record_observation(o)
        for o in _make_obs("b_win", "B", False, n=10):
            mgr.record_observation(o)
        a = mgr.analyze("b_win")
        assert a.status == "analyzed"
        assert a.winner == "A"
        assert a.results["A"]["success_rate"] == 1.0
        assert a.results["B"]["success_rate"] == 0.0

    def test_a_wins(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("a_win", min_samples=5))
        for o in _make_obs("a_win", "A", False, n=10):
            mgr.record_observation(o)
        for o in _make_obs("a_win", "B", True, n=10):
            mgr.record_observation(o)
        a = mgr.analyze("a_win")
        assert a.winner == "B"

    def test_tie_within_delta(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("tie", min_samples=10))
        for o in _make_obs("tie", "A", True, n=10):
            mgr.record_observation(o)
        for o in _make_obs("tie", "B", True, n=10):
            mgr.record_observation(o)
        # Default delta is 0.05; identical rates → tie
        a = mgr.analyze("tie")
        assert a.winner == "tie"

    def test_barely_past_delta(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("barely", min_samples=20))
        # A: 80% success
        for o in _make_obs("barely", "A", True, n=16):
            mgr.record_observation(o)
        for o in _make_obs("barely", "A", False, n=4):
            mgr.record_observation(o)
        # B: 86% success (6 pp diff — above 5% threshold)
        for o in _make_obs("barely", "B", True, n=86):
            mgr.record_observation(o)
        for o in _make_obs("barely", "B", False, n=14):
            mgr.record_observation(o)
        a = mgr.analyze("barely")
        assert a.winner == "B"

    def test_custom_delta(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("cd", min_samples=10))
        for o in _make_obs("cd", "A", True, n=10):
            mgr.record_observation(o)
        for o in _make_obs("cd", "B", True, n=10):
            mgr.record_observation(o)
        # With 50% delta threshold, 0% diff is a tie
        a = mgr.analyze("cd", winner_delta=0.5)
        assert a.winner == "tie"


# ── TestAnalyzeMetrics ─────────────────────────────────────────────


class TestAnalyzeMetrics:
    def test_includes_token_and_duration(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("metrics", min_samples=5))
        for o in _make_obs("metrics", "A", True, n=5, tokens=100, duration=50.0):
            mgr.record_observation(o)
        for o in _make_obs("metrics", "B", True, n=5, tokens=200, duration=100.0):
            mgr.record_observation(o)
        a = mgr.analyze("metrics")
        assert a.results["A"]["avg_tokens"] == 100.0
        assert a.results["B"]["avg_tokens"] == 200.0
        assert a.results["A"]["avg_duration_ms"] == 50.0
        assert a.results["B"]["avg_duration_ms"] == 100.0

    def test_includes_rating(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("rate", min_samples=5))
        for o in _make_obs("rate", "A", True, n=5, rating=4):
            mgr.record_observation(o)
        for o in _make_obs("rate", "B", True, n=5, rating=5):
            mgr.record_observation(o)
        a = mgr.analyze("rate")
        assert a.results["A"]["avg_rating"] == 4.0
        assert a.results["B"]["avg_rating"] == 5.0

    def test_avg_rating_none_when_no_ratings(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("norate", min_samples=5))
        for o in _make_obs("norate", "A", True, n=5):
            mgr.record_observation(o)
        for o in _make_obs("norate", "B", True, n=5):
            mgr.record_observation(o)
        a = mgr.analyze("norate")
        assert a.results["A"]["avg_rating"] is None


# ── TestConclude ───────────────────────────────────────────────────


class TestConclude:
    def test_conclude_with_winner(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("conc_win", min_samples=5))
        for o in _make_obs("conc_win", "A", True, n=10):
            mgr.record_observation(o)
        for o in _make_obs("conc_win", "B", False, n=10):
            mgr.record_observation(o)
        exp, analysis = mgr.conclude("conc_win")
        assert exp.status == ExperimentStatus.COMPLETED.value
        assert exp.winner == "A"
        assert exp.ended_at != ""
        # Winner promoted
        promoted = tmp_path / "conc_win" / "promoted.json"
        assert promoted.exists()
        promoted_data = json.loads(promoted.read_text())
        assert promoted_data["winner_variant_id"] == "A"

    def test_conclude_tie_leaves_running(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("conc_tie", min_samples=5))
        for o in _make_obs("conc_tie", "A", True, n=10):
            mgr.record_observation(o)
        for o in _make_obs("conc_tie", "B", True, n=10):
            mgr.record_observation(o)
        exp, analysis = mgr.conclude("conc_tie")
        assert exp.status == ExperimentStatus.RUNNING.value

    def test_conclude_insufficient_leaves_running(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("conc_insuf", min_samples=100))
        for o in _make_obs("conc_insuf", "A", True, n=5):
            mgr.record_observation(o)
        for o in _make_obs("conc_insuf", "B", True, n=5):
            mgr.record_observation(o)
        exp, analysis = mgr.conclude("conc_insuf")
        assert exp.status == ExperimentStatus.RUNNING.value
        assert analysis.status == "insufficient_samples"

    def test_conclude_unknown(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp, analysis = mgr.conclude("nope")
        assert exp is None
        assert analysis.status == "not_found"


# ── TestAbandon ────────────────────────────────────────────────────


class TestAbandon:
    def test_abandon_running(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("aban"))
        e = mgr.abandon("aban")
        assert e.status == ExperimentStatus.ABANDONED.value
        assert e.ended_at != ""

    def test_abandon_already_completed_noop(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("aban2"))
        mgr._cache["aban2"].status = ExperimentStatus.COMPLETED.value
        e = mgr.abandon("aban2")
        assert e.status == ExperimentStatus.COMPLETED.value

    def test_abandon_missing(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        assert mgr.abandon("nope") is None


# ── TestStats ──────────────────────────────────────────────────────


class TestStats:
    def test_empty(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        s = mgr.stats()
        assert s["experiment_count"] == 0
        assert s["running"] == 0
        assert s["completed"] == 0
        assert s["abandoned"] == 0
        assert s["exp_dir"] == str(tmp_path)

    def test_mixed(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("r1"))
        mgr.create(_make_exp("r2"))
        mgr.create(_make_exp("c1"))
        mgr.create(_make_exp("a1"))
        mgr._cache["c1"].status = ExperimentStatus.COMPLETED.value
        mgr._cache["a1"].status = ExperimentStatus.ABANDONED.value
        s = mgr.stats()
        assert s["experiment_count"] == 4
        assert s["running"] == 2
        assert s["completed"] == 1
        assert s["abandoned"] == 1


# ── TestSingleton ──────────────────────────────────────────────────


class TestSingleton:
    def test_singleton(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path))
        reset_ab_test_manager()
        a = get_ab_test_manager()
        b = get_ab_test_manager()
        assert a is b

    def test_reset_creates_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_EXPERIMENTS_DIR", str(tmp_path))
        a = get_ab_test_manager()
        reset_ab_test_manager()
        b = get_ab_test_manager()
        assert a is not b


# ── TestTargetEnum ─────────────────────────────────────────────────


class TestTargetEnum:
    def test_values(self):
        assert ExperimentTarget.SYSTEM_PROMPT.value == "system_prompt"
        assert ExperimentTarget.SKILL_PROMPT.value == "skill_prompt"
        assert ExperimentTarget.TOOL_DEFAULT.value == "tool_default"
        assert ExperimentTarget.USER_REMINDER.value == "user_reminder"


# ── TestThreeVariants ──────────────────────────────────────────────


class TestThreeVariants:
    """3+ variant experiments: analysis picks top-2 by sample size."""

    def test_three_variants_creation(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = Experiment(
            id="exp_3v", name="3-way", description="",
            target="system_prompt", target_key="intro",
            variants=[
                ExperimentVariant(id="A", name="control", config={}),
                ExperimentVariant(id="B", name="treatment1", config={}),
                ExperimentVariant(id="C", name="treatment2", config={}),
            ],
            min_samples=3,
        )
        mgr.create(exp)
        # Each user gets exactly one variant (deterministic)
        v = mgr.assign_variant("exp_3v", "alice")
        assert v.id in {"A", "B", "C"}

    def test_three_variant_analysis_picks_top2(self, tmp_path):
        """When A and B are both present, the analyzer compares them
        specifically (it's the canonical A/B test case). C's data is
        still recorded but not used for the winner decision."""
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_3v", variants=[
            ExperimentVariant(id="A", name="control", config={}),
            ExperimentVariant(id="B", name="treatment1", config={}),
            ExperimentVariant(id="C", name="treatment2", config={}),
        ], min_samples=3)
        mgr.create(exp)
        # A: 5 samples, 80% success
        for o in _make_obs("exp_3v", "A", True, 4):
            mgr.record_observation(o)
        for o in _make_obs("exp_3v", "A", False, 1):
            mgr.record_observation(o)
        # B: 5 samples, 100% success
        for o in _make_obs("exp_3v", "B", True, 5):
            mgr.record_observation(o)
        # C: 10 samples, 30% success (irrelevant to A vs B comparison)
        for o in _make_obs("exp_3v", "C", True, 3):
            mgr.record_observation(o)
        for o in _make_obs("exp_3v", "C", False, 7):
            mgr.record_observation(o)
        analysis = mgr.analyze("exp_3v")
        # A vs B (specific): B (100%) - A (80%) = 20% > delta → B wins
        assert analysis.status == "analyzed"
        assert "B" in analysis.results
        assert "A" in analysis.results
        assert "C" in analysis.results
        assert analysis.winner == "B"

    def test_three_variant_no_a_uses_top2_by_n(self, tmp_path):
        """When A is not in the experiment, fall back to top-2 by n."""
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_no_a", variants=[
            ExperimentVariant(id="B", name="first", config={}),
            ExperimentVariant(id="C", name="second", config={}),
            ExperimentVariant(id="D", name="third", config={}),
        ], min_samples=3)
        mgr.create(exp)
        # B: 5 samples, 30% success
        for o in _make_obs("exp_no_a", "B", True, 1):
            mgr.record_observation(o)
        for o in _make_obs("exp_no_a", "B", False, 4):
            mgr.record_observation(o)
        # C: 8 samples, 80% success
        for o in _make_obs("exp_no_a", "C", True, 6):
            mgr.record_observation(o)
        for o in _make_obs("exp_no_a", "C", False, 2):
            mgr.record_observation(o)
        # D: 4 samples, 50% success
        for o in _make_obs("exp_no_a", "D", True, 2):
            mgr.record_observation(o)
        for o in _make_obs("exp_no_a", "D", False, 2):
            mgr.record_observation(o)
        analysis = mgr.analyze("exp_no_a")
        # Top 2 by n: C (8) and B (5); C 80% vs B 30% → C wins
        assert analysis.status == "analyzed"
        assert analysis.winner == "C"


# ── TestPromotedJson ───────────────────────────────────────────────


class TestPromotedJson:
    def test_conclude_writes_promoted_file(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_promote", min_samples=2)
        mgr.create(exp)
        # 4 successes for B, 2 failures for A → B clearly wins
        for o in _make_obs("exp_promote", "B", True, 4):
            mgr.record_observation(o)
        for o in _make_obs("exp_promote", "A", False, 2):
            mgr.record_observation(o)
        result_exp, analysis = mgr.conclude("exp_promote")
        assert result_exp.status == ExperimentStatus.COMPLETED.value
        assert result_exp.winner == "B"
        # promoted.json should exist
        promoted_path = tmp_path / "exp_promote" / "promoted.json"
        assert promoted_path.exists()
        promoted = json.loads(promoted_path.read_text())
        assert promoted["experiment_id"] == "exp_promote"
        assert promoted["winner_variant_id"] == "B"
        assert promoted["config"] == {"new_content": "You are a treatment agent."}
        assert "promoted_at" in promoted

    def test_promoted_file_has_target_metadata(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_meta", min_samples=2,
                        target=ExperimentTarget.TOOL_DEFAULT.value,
                        target_key="read_file.timeout")
        mgr.create(exp)
        for o in _make_obs("exp_meta", "A", True, 2):
            mgr.record_observation(o)
        for o in _make_obs("exp_meta", "B", False, 2):
            mgr.record_observation(o)
        mgr.conclude("exp_meta")
        promoted = json.loads((tmp_path / "exp_meta" / "promoted.json").read_text())
        assert promoted["target"] == "tool_default"
        assert promoted["target_key"] == "read_file.timeout"


# ── TestPersistenceAndReload ──────────────────────────────────────


class TestPersistenceAndReload:
    def test_experiment_persists_after_manager_restart(self, tmp_path):
        """After a manager restart, the experiment should be loaded from
        disk via _load_all()."""
        mgr1 = ABTestManager(exp_dir=tmp_path)
        mgr1.create(_make_exp("exp_persist"))
        # Verify file is on disk
        assert (tmp_path / "exp_persist" / "experiment.json").exists()
        # New manager, same dir → should pick up the experiment
        mgr2 = ABTestManager(exp_dir=tmp_path)
        assert mgr2.get("exp_persist") is not None
        assert mgr2.get("exp_persist").name == "Test exp_persist"

    def test_observations_persist_after_manager_restart(self, tmp_path):
        mgr1 = ABTestManager(exp_dir=tmp_path)
        mgr1.create(_make_exp("exp_obs_persist"))
        for o in _make_obs("exp_obs_persist", "A", True, 3):
            mgr1.record_observation(o)
        mgr2 = ABTestManager(exp_dir=tmp_path)
        obs = mgr2.observations("exp_obs_persist")
        assert len(obs) == 3
        assert all(o.success is True for o in obs)

    def test_malformed_experiment_file_does_not_crash_load(self, tmp_path):
        exp_dir = tmp_path / "exp_broken"
        exp_dir.mkdir()
        (exp_dir / "experiment.json").write_text("not valid json")
        # Should not raise
        mgr = ABTestManager(exp_dir=tmp_path)
        # The broken exp is skipped; the rest still load
        assert mgr.get("exp_broken") is None

    def test_persisted_conclusion_preserved(self, tmp_path):
        mgr1 = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_done", min_samples=2)
        mgr1.create(exp)
        for o in _make_obs("exp_done", "A", False, 2):
            mgr1.record_observation(o)
        for o in _make_obs("exp_done", "B", True, 2):
            mgr1.record_observation(o)
        mgr1.conclude("exp_done")
        # Reload
        mgr2 = ABTestManager(exp_dir=tmp_path)
        reloaded = mgr2.get("exp_done")
        assert reloaded.status == ExperimentStatus.COMPLETED.value
        assert reloaded.winner == "B"


# ── TestWeightedDistribution ──────────────────────────────────────


class TestWeightedDistribution:
    def test_heavy_a_weight(self, tmp_path):
        """When A is weighted 9, B is weighted 1 → ~90% of users get A."""
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_weighted", variants=[
            ExperimentVariant(id="A", name="heavy", config={}, weight=9.0),
            ExperimentVariant(id="B", name="light", config={}, weight=1.0),
        ])
        mgr.create(exp)
        counts = {"A": 0, "B": 0}
        for i in range(200):
            v = mgr.assign_variant("exp_weighted", f"user_{i}")
            counts[v.id] += 1
        # Allow generous tolerance
        assert counts["A"] > counts["B"] * 3
        assert counts["A"] + counts["B"] == 200

    def test_zero_weight_never_picked(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_zero", variants=[
            ExperimentVariant(id="A", name="active", config={}, weight=1.0),
            ExperimentVariant(id="B", name="dead", config={}, weight=0.0),
        ])
        mgr.create(exp)
        for i in range(50):
            v = mgr.assign_variant("exp_zero", f"u{i}")
            # B can only be picked if cumulative math goes wrong; should be
            # unreachable since weight=0 contributes 0 to the cumulative.
            assert v.id == "A"

    def test_all_zero_weights_falls_back_to_last(self, tmp_path):
        """All zero weights → sum is 0 → fallback to 1.0 → last variant
        is picked for every user (cumulative never reaches bucket)."""
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("exp_zeroall", variants=[
            ExperimentVariant(id="A", name="a", config={}, weight=0.0),
            ExperimentVariant(id="B", name="b", config={}, weight=0.0),
        ])
        mgr.create(exp)
        v = mgr.assign_variant("exp_zeroall", "alice")
        # Implementation divides by 1.0 fallback → 0 cumulative → returns last
        assert v.id == "B"


# ── TestObservationWithRating ─────────────────────────────────────


class TestObservationWithRating:
    def test_avg_rating_computed(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("exp_rate"))
        for o in _make_obs("exp_rate", "A", True, 3, rating=4):
            mgr.record_observation(o)
        for o in _make_obs("exp_rate", "A", True, 1, rating=5):
            mgr.record_observation(o)
        for o in _make_obs("exp_rate", "B", True, 2, rating=5):
            mgr.record_observation(o)
        for o in _make_obs("exp_rate", "B", True, 2, rating=3):
            mgr.record_observation(o)
        analysis = mgr.analyze("exp_rate", min_samples=4)
        # A: avg 4.25 (4,4,4,5), B: avg 4.0 (5,5,3,3)
        # success_rate: A 100%, B 100% → tied
        assert analysis.results["A"]["avg_rating"] == 4.25
        assert analysis.results["B"]["avg_rating"] == 4.0

    def test_avg_rating_none_when_no_ratings(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("exp_no_rate", min_samples=2))
        for o in _make_obs("exp_no_rate", "A", True, 2, rating=None):
            mgr.record_observation(o)
        for o in _make_obs("exp_no_rate", "B", False, 2, rating=None):
            mgr.record_observation(o)
        analysis = mgr.analyze("exp_no_rate", min_samples=2)
        assert analysis.results["A"]["avg_rating"] is None
        assert analysis.results["B"]["avg_rating"] is None

    def test_partial_ratings(self, tmp_path):
        """Some observations have ratings, others don't. Avg should be
        computed only over the rated ones."""
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("exp_partial", min_samples=3))
        for o in _make_obs("exp_partial", "A", True, 1, rating=5):
            mgr.record_observation(o)
        for o in _make_obs("exp_partial", "A", True, 2, rating=None):
            mgr.record_observation(o)
        for o in _make_obs("exp_partial", "B", True, 3, rating=4):
            mgr.record_observation(o)
        analysis = mgr.analyze("exp_partial", min_samples=3)
        # A: only 1 rating (5) → avg = 5.0
        assert analysis.results["A"]["avg_rating"] == 5.0
        # B: 3 ratings all 4 → avg = 4.0
        assert analysis.results["B"]["avg_rating"] == 4.0


# ── TestExperimentValidation ─────────────────────────────────────


class TestExperimentValidation:
    def test_create_rejects_missing_id_generates_one(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = _make_exp("")  # Empty id
        mgr.create(exp)
        assert exp.id.startswith("exp_")
        assert len(exp.id) > 4

    def test_create_assigns_variant_ids_if_missing(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        exp = Experiment(
            id="exp_auto", name="", description="",
            target="system_prompt", target_key="x",
            variants=[
                ExperimentVariant(id="", name="first", config={}),
                ExperimentVariant(id="", name="second", config={}),
                ExperimentVariant(id="", name="third", config={}),
            ],
        )
        mgr.create(exp)
        ids = [v.id for v in exp.variants]
        assert ids == ["A", "B", "C"]

    def test_get_returns_none_for_unknown(self, tmp_path):
        mgr = ABTestManager(exp_dir=tmp_path)
        assert mgr.get("does_not_exist") is None

    def test_variant_by_id_returns_none_for_unknown(self, tmp_path):
        exp = _make_exp("exp_v")
        assert exp.variant_by_id("Z") is None
        assert exp.variant_by_id("A") is not None

    def test_to_from_dict_round_trip_full(self, tmp_path):
        original = _make_exp("exp_full")
        original.metadata = {"key": "value", "nested": {"a": 1}}
        d = original.to_dict()
        restored = Experiment.from_dict(d)
        assert restored.id == original.id
        assert restored.metadata == original.metadata
        assert len(restored.variants) == len(original.variants)

    def test_observation_to_from_dict_preserves_metadata(self, tmp_path):
        obs = ExperimentObservation(
            experiment_id="e", variant_id="A", user_id="u",
            task="t", success=True, token_input=10, token_output=20,
            duration_ms=100.0, user_rating=5,
            metadata={"trace_id": "abc", "model": "gpt-4o"},
        )
        d = obs.to_dict()
        assert d["metadata"] == {"trace_id": "abc", "model": "gpt-4o"}
        restored = ExperimentObservation.from_dict(d)
        assert restored.metadata == {"trace_id": "abc", "model": "gpt-4o"}


# ── TestConcurrentObservations ────────────────────────────────────


class TestConcurrentObservations:
    def test_concurrent_record_observation_no_loss(self, tmp_path):
        """20 parallel record_observation calls should all show up in the
        file (no append-mode clobbering)."""
        import threading
        mgr = ABTestManager(exp_dir=tmp_path)
        mgr.create(_make_exp("exp_concurrent"))
        def record_one(i):
            mgr.record_observation(ExperimentObservation(
                experiment_id="exp_concurrent", variant_id="A",
                user_id=f"u{i}", task=f"t{i}", success=True,
                token_input=10, token_output=10, duration_ms=10.0,
            ))
        threads = [threading.Thread(target=record_one, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All 20 should be on disk (no clobbering)
        obs = mgr.observations("exp_concurrent")
        assert len(obs) == 20
